"""生成 README 用的「每日预测图片」示例图。

脱离 AstrBot 运行，只依赖 Pillow 与 `services/` 里的纯函数模块：

```bash
python scripts/render_readme_preview.py [输出路径]
```

默认输出 `docs/images/daily-report-preview.png`（450×600，深色主题）。
示例数据是**固定值**，不来自上游 API；因为不含时间戳，重复生成的字节一致，
git diff 保持干净。署名行与生成器署名由渲染器自动内嵌，满足 AGPL-3.0 署名要求。
"""

from __future__ import annotations

import io
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from PIL import Image

if TYPE_CHECKING:  # 仅类型标注需要，运行时不导入（避免模块顶层依赖 services）
    from services.models import ReportData

#: 仓库根目录（本脚本位于 `scripts/` 下）
REPO_ROOT = Path(__file__).resolve().parent.parent

#: 默认输出路径
DEFAULT_OUTPUT = REPO_ROOT / "docs" / "images" / "daily-report-preview.png"

#: README 中展示用的降采样尺寸（原始 900×1200 只用于渲染，不入库）
PREVIEW_SIZE = (450, 600)

#: 量化色板位数（扁平设计下 256 色已肉眼无损）
PALETTE_COLORS = 256

#: 示例数据的展示时区（与插件默认 `daily_report_timezone` 一致）
DISPLAY_ZONE = ZoneInfo("Asia/Shanghai")


def sample_data() -> ReportData:
    """构造固定的示例数据（仅用于演示卡片排版，非真实预测）。"""
    from services.models import ReportData

    return ReportData(
        rounded_24h=42,
        rounded_48h=58,
        confidence="medium",
        updated_at=datetime(2026, 10, 22, 1, 5, tzinfo=UTC),
        business_date="2026-10-22",
        last_reset_at=datetime(2026, 10, 18, 22, 15, tzinfo=UTC),
        watch_status="watch",
        watch_window="今日 20:00—22:00（北京时间）",
        stale=False,
        theme="dark",
    )


def build_png() -> bytes:
    """渲染示例卡片、降采样并量化，返回 PNG 字节。"""
    from services.report_renderer import render_report_png

    original = render_report_png(sample_data(), tz=DISPLAY_ZONE)
    with Image.open(io.BytesIO(original)) as image:
        resized = image.resize(PREVIEW_SIZE, Image.Resampling.LANCZOS)
        # 卡片是扁平设计，256 色自适应量化后体积从约 85KB 降到约 33KB，
        # 平均色差 0.16/255，肉眼无感；不用 WebP 是为兼容未知的 README 渲染器。
        palette = resized.convert("P", palette=Image.ADAPTIVE, colors=PALETTE_COLORS)
        buffer = io.BytesIO()
        palette.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()


def main(argv: list[str]) -> int:
    """入口：生成并写出 PNG，返回进程退出码。"""
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)

    output = Path(argv[1]).resolve() if len(argv) > 1 else DEFAULT_OUTPUT
    png = build_png()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(png)
    print(f"已生成 {output}（{len(png)} 字节，{PREVIEW_SIZE[0]}×{PREVIEW_SIZE[1]}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
