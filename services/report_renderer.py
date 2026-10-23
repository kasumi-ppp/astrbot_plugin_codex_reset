"""每日预测 PNG 渲染器（Pillow，900×1200 竖版，深色科技风 / 浅色简洁风双主题）。

硬约束（PRD §3 P03/P05/P08/P11）：
- 概率值**必须**直接来自 API 快照，本模块不做任何推断、取整调整或示例兜底；
- 必须展示未来 24h 概率、未来 48h 概率、数据更新时间、实验性预测说明；
- 必须保留 `codex-reset.com` 来源署名（许可要求，不可关闭）；
- 最近一次重置只显示**已确认**记录，没有可靠记录时显示「暂无记录」（P04）；
- 官方预告区只呈现上游状态，没有可靠窗口时不预测具体小时数（P05）；
- 字体/资源/渲染异常一律抛 `ReportRenderError`，由调用方降级为文本（P10）。

本模块零 I/O（不读网络、不读数据库），仅依赖 Pillow 与本地字体文件，便于单测。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .clock import ensure_utc
from .errors import SITE_URL

logger = logging.getLogger(__name__)

CANVAS_WIDTH = 900
CANVAS_HEIGHT = 1200
"""建议画布尺寸（PRD §3 P08：900×1200 或相近竖版比例）。"""

DARK_THEME = "dark"
LIGHT_THEME = "light"
DEFAULT_THEME = DARK_THEME
THEME_NAMES: tuple[str, ...] = (DARK_THEME, LIGHT_THEME)

SOURCE_TEXT = f"数据来源：{SITE_URL}"
"""来源署名（与 `notifications.SOURCE_LINE` 同源，图片中不可省略）。"""

EXPERIMENTAL_TEXT = "实验性预测，不代表官方承诺"
NO_RECORD_TEXT = "暂无记录"
NO_WINDOW_TEXT = "暂无明确预计时段"

WATCH_STATUS_TEXT: dict[str, str] = {
    "none": "暂无明确预告",
    "watch": "存在有效预告",
    "confirmed": "已确认重置",
}

CONFIDENCE_CN: dict[str, str] = {"low": "低", "medium": "中", "high": "高"}


class ReportRenderError(Exception):
    """图片渲染失败（字体缺失 / 资源异常 / Pillow 异常），调用方应降级为文本。"""

    def __init__(self, message: str, *, reason: str = "render_error") -> None:
        super().__init__(message)
        self.reason = reason


# ---------------------------------------------------------------------------
# 主题
# ---------------------------------------------------------------------------
THEMES: dict[str, dict[str, tuple[int, int, int]]] = {
    DARK_THEME: {
        "bg": (13, 17, 23),
        "card": (22, 28, 37),
        "card_border": (45, 54, 68),
        "title": (235, 242, 252),
        "body": (199, 210, 225),
        "muted": (134, 146, 163),
        "accent": (88, 166, 255),
        "bar_bg": (38, 46, 58),
        "bar_24": (56, 189, 248),
        "bar_48": (167, 139, 250),
        "warn": (250, 204, 21),
        "divider": (45, 54, 68),
    },
    LIGHT_THEME: {
        "bg": (243, 246, 250),
        "card": (255, 255, 255),
        "card_border": (214, 221, 231),
        "title": (22, 30, 42),
        "body": (58, 68, 82),
        "muted": (116, 126, 141),
        "accent": (26, 115, 232),
        "bar_bg": (226, 232, 240),
        "bar_24": (14, 145, 205),
        "bar_48": (124, 92, 224),
        "warn": (176, 118, 12),
        "divider": (214, 221, 231),
    },
}


def theme_colors(theme: str) -> dict[str, tuple[int, int, int]]:
    """取主题配色；未知主题回落深色（不抛异常，调用方已在上游校验并告警）。"""
    return THEMES.get(str(theme).strip().lower(), THEMES[DEFAULT_THEME])


# ---------------------------------------------------------------------------
# 字体
# ---------------------------------------------------------------------------
FONT_CANDIDATES: tuple[str, ...] = (
    # Windows 优先：微软雅黑 / 黑体 / 等线 / Noto Sans SC
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/Deng.ttf",
    "C:/Windows/Fonts/NotoSansSC-VF.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    # Linux 常见 CJK 字体
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
)

FONT_SEARCH_DIRS: tuple[str, ...] = (
    "C:/Windows/Fonts",
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/System/Library/Fonts",
    "/Library/Fonts",
)

FONT_SEARCH_PATTERNS: tuple[str, ...] = (
    "*msyh*",
    "*simhei*",
    "*NotoSans*CJK*",
    "*NotoSansSC*",
    "*wqy*",
    "*DroidSansFallback*",
    "*PingFang*",
)

#: 包内内置中文字体（随插件分发，不依赖宿主系统字体）。
BUNDLED_FONT_FILENAME = "NotoSansCJKsc-Regular.otf"


def bundled_font_path() -> str | None:
    """返回包内内置中文字体的绝对路径；文件不存在时返回 None。

    基于 `Path(__file__)` 定位，保证插件被打包 / 软链 / 任意工作目录下都能找到
    `resources/fonts/NotoSansCJKsc-Regular.otf`。
    """
    candidate = (
        Path(__file__).resolve().parent.parent / "resources" / "fonts" / BUNDLED_FONT_FILENAME
    )
    return str(candidate) if candidate.is_file() else None


def _resolve_system_font_path() -> str | None:
    """仅在系统字体候选 / 搜索目录中定位中文字体（不含内置字体）。"""
    for raw in FONT_CANDIDATES:
        candidate = Path(raw)
        if candidate.is_file():
            return str(candidate)
    for directory in FONT_SEARCH_DIRS:
        base = Path(directory)
        if not base.is_dir():
            continue
        for pattern in FONT_SEARCH_PATTERNS:
            try:
                for found in sorted(base.glob(pattern)):
                    if found.is_file() and found.suffix.lower() in {".ttf", ".ttc", ".otf"}:
                        return str(found)
            except OSError:  # pragma: no cover - 目录不可读
                continue
    return None


def _resolve_font_path(explicit: str | None = None) -> str | None:
    """定位一个可用的中文字体文件；找不到返回 None（调用方抛 `ReportRenderError`）。

    优先级：显式 `explicit` 参数 > 包内内置字体 > 系统字体候选 / 搜索目录。
    """
    if explicit:
        candidate = Path(explicit)
        if candidate.is_file():
            return str(candidate)
        logger.warning("[codex-reset][report] 指定字体不存在：%s", explicit)
    bundled = bundled_font_path()
    if bundled is not None:
        return bundled
    return _resolve_system_font_path()


def image_environment_status() -> tuple[bool, str]:
    """图片环境就绪状态（供 `/cr status` 展示）。

    返回 `(是否就绪, 中文说明)`：
    - 内置字体存在 → `(True, "就绪（内置中文字体）")`；
    - 只有系统字体 → `(True, "就绪（系统字体）")`；
    - 均缺失 → `(False, "未就绪（缺少中文字体）")`。
    """
    if bundled_font_path() is not None:
        return True, "就绪（内置中文字体）"
    if _resolve_system_font_path() is not None:
        return True, "就绪（系统字体）"
    return False, "未就绪（缺少中文字体）"


class FontSet:
    """一套按字号缓存的字体（同一渲染器内复用，避免反复解析大字体文件）。"""

    __slots__ = ("_path", "_cache")

    def __init__(self, path: str | None = None) -> None:
        resolved = _resolve_font_path(path)
        if resolved is None:
            raise ReportRenderError(
                "未找到可用的中文字体（已尝试系统字体目录），无法生成预测图片",
                reason="font_missing",
            )
        self._path = resolved
        self._cache: dict[int, Any] = {}

    @property
    def path(self) -> str:
        """实际使用的字体文件路径（排障用）。"""
        return self._path

    def get(self, size: int) -> Any:
        """取指定字号的字体对象（带缓存）。"""
        key = max(8, int(size))
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        try:
            font = ImageFont.truetype(self._path, key)
        except Exception as exc:  # 字体文件损坏 / 不是有效字体
            raise ReportRenderError(f"字体加载失败：{self._path}（{exc}）", reason="font_error") from exc
        self._cache[key] = font
        return font


# ---------------------------------------------------------------------------
# 绘图辅助
# ---------------------------------------------------------------------------
def _text_size(draw: ImageDraw.ImageDraw, text: str, font: Any) -> tuple[int, int]:
    """测量文本尺寸（Pillow ≥10 的 `textbbox` 口径）。"""
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0], box[3] - box[1]


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: Any, max_width: int) -> list[str]:
    """按像素宽度折行：中文逐字断行，英文/数字按空格断词（超长词强制切分）。"""
    if not text:
        return []
    lines: list[str] = []
    current = ""
    for token in _tokenize(text):
        candidate = f"{current}{token}"
        if current and _text_size(draw, candidate, font)[0] > max_width:
            lines.append(current.rstrip())
            current = token.lstrip() if token.startswith(" ") else token
        else:
            current = candidate
    if current.strip():
        lines.append(current.rstrip())
    return lines or [""]


def _tokenize(text: str) -> list[str]:
    """把文本切成最小可断单元：连续 ASCII 视为一个词，其余逐字符。"""
    tokens: list[str] = []
    buffer = ""
    for char in text:
        if char.isspace() or ord(char) < 128:
            if buffer:
                tokens.append(buffer)
                buffer = ""
            tokens.append(char)
        else:
            buffer += char
    if buffer:
        tokens.append(buffer)
    return tokens


def _card(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    colors: Mapping[str, tuple[int, int, int]],
    *,
    radius: int = 24,
) -> None:
    """画一张圆角卡片（填充 + 描边）。"""
    draw.rounded_rectangle(box, radius=radius, fill=colors["card"], outline=colors["card_border"], width=2)


def _progress(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    colors: Mapping[str, tuple[int, int, int]],
    percent: int,
    color_key: str,
) -> None:
    """画进度条：`percent` 已被上游钳制到 0-100。"""
    x0, y0, x1, y1 = box
    draw.rounded_rectangle(box, radius=(y1 - y0) // 2, fill=colors["bar_bg"])
    width = x1 - x0
    filled = int(round(width * max(0, min(100, int(percent))) / 100))
    if filled > 4:
        draw.rounded_rectangle(
            (x0, y0, x0 + filled, y1), radius=(y1 - y0) // 2, fill=colors[color_key]
        )


def _clamp_percent(value: Any) -> int:
    """把概率钳制到 0-100（**不改数据源语义**，只防止异常值破坏排版）。"""
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        logger.warning("[codex-reset][report] 概率值非法（%r），按 0 处理", value)
        return 0


# ---------------------------------------------------------------------------
# 数据 → 报告字段
# ---------------------------------------------------------------------------
def report_payload(data: Any) -> dict[str, Any]:
    """`ReportData` → payload（渲染器与文本降级模板共用同一份字段）。"""
    if hasattr(data, "as_payload"):
        return data.as_payload()  # type: ignore[no-any-return]
    if isinstance(data, Mapping):
        return dict(data)
    raise ReportRenderError(f"不支持的报告数据类型：{type(data)!r}", reason="bad_input")


def _format_moment(value: Any, tz: Any, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """把 payload 里的 ISO 时间渲染为本地时间字符串；缺失返回空串。"""
    if value is None:
        return ""
    if isinstance(value, datetime):
        moment = ensure_utc(value)
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return ""
        moment = ensure_utc(parsed)
    else:
        return ""
    return moment.astimezone(tz).strftime(fmt)


def watch_status_text(status: Any) -> str:
    """预告状态 → 中文（未识别值原样保留，绝不编造「已确认」）。"""
    key = str(status or "none").strip().lower()
    return WATCH_STATUS_TEXT.get(key, NO_WINDOW_TEXT if key == "none" else str(status))


# ---------------------------------------------------------------------------
# 主渲染入口
# ---------------------------------------------------------------------------
def render_report_png(data: Any, *, tz: Any, font_path: str | None = None) -> bytes:
    """渲染每日预测 PNG；任何异常都包装成 `ReportRenderError`（P10 触发文本降级）。"""
    payload = report_payload(data)
    try:
        return _render(payload, tz=tz, font_path=font_path)
    except ReportRenderError:
        raise
    except Exception as exc:
        raise ReportRenderError(f"预测图片渲染失败：{exc}", reason="render_error") from exc


def _render(payload: Mapping[str, Any], *, tz: Any, font_path: str | None) -> bytes:
    theme = str(payload.get("theme") or DEFAULT_THEME).strip().lower()
    colors = theme_colors(theme)
    fonts = FontSet(font_path)

    image = Image.new("RGB", (CANVAS_WIDTH, CANVAS_HEIGHT), colors["bg"])
    draw = ImageDraw.Draw(image)

    margin = 48
    left = margin
    right = CANVAS_WIDTH - margin
    content_width = right - left

    title_font = fonts.get(42)
    subtitle_font = fonts.get(24)
    label_font = fonts.get(28)
    number_font = fonts.get(84)
    body_font = fonts.get(26)
    small_font = fonts.get(21)
    footer_font = fonts.get(22)

    # -- 头部 ----------------------------------------------------------
    top = 54
    draw.text((left, top), "Codex 重置概率预测", font=title_font, fill=colors["title"])
    business_date = str(payload.get("business_date") or "").strip()
    subtitle = f"每日预测 · {business_date}" if business_date else "每日预测"
    if bool(payload.get("manual")):
        subtitle = f"{subtitle} · 手动生成"
    if bool(payload.get("test")):
        subtitle = f"{subtitle} · 测试"
    draw.text((left, top + 58), subtitle, font=subtitle_font, fill=colors["muted"])

    # -- 24h / 48h 概率卡 ----------------------------------------------
    y = top + 116
    card_height = 208
    for label, key, color_key in (
        ("未来 24 小时", "rounded_24h", "bar_24"),
        ("未来 48 小时", "rounded_48h", "bar_48"),
    ):
        box = (left, y, right, y + card_height)
        _card(draw, box, colors)
        percent = _clamp_percent(payload.get(key))

        draw.text((left + 32, y + 26), label, font=label_font, fill=colors["body"])
        number_text = f"{percent}%"
        number_width, _ = _text_size(draw, number_text, number_font)
        draw.text(
            (right - 32 - number_width, y + 6), number_text, font=number_font, fill=colors[color_key]
        )
        _progress(draw, (left + 32, y + 132, right - 32, y + 156), colors, percent, color_key)
        draw.text(
            (left + 32, y + 166),
            f"重置概率 {percent} / 100",
            font=small_font,
            fill=colors["muted"],
        )
        y += card_height + 20

    # -- 最近重置 + 官方预告 -------------------------------------------
    info_height = 224
    box = (left, y, right, y + info_height)
    _card(draw, box, colors)
    draw.text((left + 32, y + 24), "最近一次已确认全球重置", font=label_font, fill=colors["body"])
    reset_text = _format_moment(payload.get("last_reset_at"), tz) or NO_RECORD_TEXT
    draw.text((left + 32, y + 62), reset_text, font=body_font, fill=colors["accent"])
    draw.line((left + 32, y + 118, right - 32, y + 118), fill=colors["divider"], width=2)
    status = watch_status_text(payload.get("watch_status"))
    draw.text((left + 32, y + 138), f"官方预告状态：{status}", font=body_font, fill=colors["body"])
    window = str(payload.get("watch_window") or "").strip()
    if str(payload.get("watch_status")) == "watch":
        window_text = f"预计窗口：{window}" if window else f"预计窗口：{NO_WINDOW_TEXT}"
    else:
        window_text = "没有可靠窗口时不预测具体小时数"
    draw.text((left + 32, y + 180), window_text, font=small_font, fill=colors["muted"])
    y += info_height + 24

    # -- 数据说明卡（P11：数据更新时间 + 实验性声明） --------------------
    note_height = 196
    box = (left, y, right, y + note_height)
    _card(draw, box, colors)
    updated_text = _format_moment(payload.get("updated_at"), tz) or "未知"
    # P11：置信度只展示上游给出的 low/medium/high；缺失或未识别时**整行省略**，
    # 绝不自行推断或用 `unknown` 之类的内部占位值顶替。
    confidence_label = confidence_text(payload.get("confidence"))
    draw.text((left + 32, y + 26), f"数据更新时间：{updated_text}", font=body_font, fill=colors["body"])
    if confidence_label:
        draw.text(
            (left + 32, y + 66), f"模型置信度：{confidence_label}", font=body_font, fill=colors["body"]
        )
    draw.line((left + 32, y + 112, right - 32, y + 112), fill=colors["divider"], width=2)
    note_y = y + 130
    if bool(payload.get("stale")):
        draw.text((left + 32, note_y), "数据可能已过时，仅供参考", font=small_font, fill=colors["warn"])
        note_y += 34
    for line in _wrap(draw, f"提示：{EXPERIMENTAL_TEXT}。", small_font, content_width - 64):
        draw.text((left + 32, note_y), line, font=small_font, fill=colors["warn"])
        note_y += 34
    y += note_height + 24

    # -- 署名（不可关闭） ----------------------------------------------
    draw.text((left, CANVAS_HEIGHT - 76), SOURCE_TEXT, font=footer_font, fill=colors["muted"])
    draw.text(
        (left, CANVAS_HEIGHT - 46),
        "由 astrbot-plugin-codex-reset 生成",
        font=small_font,
        fill=colors["muted"],
    )

    buffer = _encode(image)
    logger.info(
        "[codex-reset][report] 渲染完成 theme=%s bytes=%s date=%s",
        theme,
        len(buffer),
        business_date,
    )
    return buffer


def _encode(image: Image.Image) -> bytes:
    """把图片编码为 PNG 字节（异常由调用方统一包装）。"""
    stream = BytesIO()
    image.save(stream, format="PNG", optimize=True)
    return stream.getvalue()


# ---------------------------------------------------------------------------
# 文本降级（P10）：图片不可用时仍给出完整要素
# ---------------------------------------------------------------------------
def render_report_text_lines(payload: Mapping[str, Any], *, tz: Any) -> list[str]:
    """文本降级主体（不含署名行，由 `notifications._compose` 统一追加）。"""
    updated_text = _format_moment(payload.get("updated_at"), tz) or "未知"
    reset_text = _format_moment(payload.get("last_reset_at"), tz) or NO_RECORD_TEXT
    status = watch_status_text(payload.get("watch_status"))
    lines = [
        f"未来 24 小时：{_clamp_percent(payload.get('rounded_24h'))}%",
        f"未来 48 小时：{_clamp_percent(payload.get('rounded_48h'))}%",
        f"数据更新时间：{updated_text}",
        f"最近一次已确认全球重置：{reset_text}",
        f"官方预告状态：{status}",
    ]
    if str(payload.get("watch_status")) == "watch":
        window = str(payload.get("watch_window") or "").strip()
        lines.append(f"预计窗口：{window or NO_WINDOW_TEXT}")
    return lines


def confidence_text(value: Any) -> str:
    """把上游置信度转成中文；无法确认时返回空串（**整行省略**，不自行推断）。

    P11 / C02：置信度「有可靠数据时」才展示。`unknown`、缺失、未识别枚举一律不显示。
    """
    return CONFIDENCE_CN.get(str(value or "").strip(), "")


def ensure_theme(value: Any) -> str:
    """把任意主题值收敛到合法主题（非法回落深色）。"""
    text = str(value or "").strip().lower()
    if text in THEME_NAMES:
        return text
    if text:
        logger.warning("[codex-reset][report] 未知主题 %r，已回落 %s", value, DEFAULT_THEME)
    return DEFAULT_THEME


def merge_lines(*groups: Sequence[str]) -> list[str]:
    """合并多个文本片段（供调用方拼装降级消息）。"""
    merged: list[str] = []
    for group in groups:
        merged.extend(group)
    return merged


__all__ = [
    "BUNDLED_FONT_FILENAME",
    "CANVAS_HEIGHT",
    "CANVAS_WIDTH",
    "CONFIDENCE_CN",
    "DARK_THEME",
    "DEFAULT_THEME",
    "EXPERIMENTAL_TEXT",
    "FONT_CANDIDATES",
    "LIGHT_THEME",
    "NO_RECORD_TEXT",
    "NO_WINDOW_TEXT",
    "ReportRenderError",
    "SOURCE_TEXT",
    "THEMES",
    "THEME_NAMES",
    "WATCH_STATUS_TEXT",
    "bundled_font_path",
    "confidence_text",
    "ensure_theme",
    "image_environment_status",
    "merge_lines",
    "render_report_png",
    "render_report_text_lines",
    "report_payload",
    "theme_colors",
    "watch_status_text",
]
