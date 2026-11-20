# AI 额度雷达

**把 AI 额度重置公告推送到 QQ 群。** 群管理员可以按产品订阅，通知附带适用范围和来源链接。

`v1.7.1` · AstrBot `>=4.16,<5` · Python `>=3.12` · QQ 适配器 `aiocqhttp`（OneBot v11）

## 能监控什么

| 产品 | 订阅名 | 推送内容 |
|---|---|---|
| Codex | `codex` | 重置确认、预警、额外权益和 24/48 小时实验性预测，可选每日重置状态图片 |
| Claude / Claude Code | `claude` | 公开重置公告、可能重置提醒和可领取的重置权益 |
| Grok Bot | `grok-bot` | 面向用户群体的重置信号、确认和权益 |
| Grok / Build | `grok-build` | Grok 与 Grok Build 共享额度的公开重置信号、确认和权益 |
| Antigravity Gemini | `antigravity-gemini` | Antigravity 内 Gemini 额度的规则预警、预告和确认 |
| Google Gemini | `gemini` | QuotaResets 收录的 Google Gemini 公开重置信号与确认 |

首次成功采集只记录历史。之后，新信号发现即入队，消息会区分「可能重置」「已预告」和「已重置」。群管理员可用 `/cr set watch off` 关闭预警和预告。Claude 的第三方估算用群的 `p24` / `p48` 门槛，同一个重置周期最多提醒每群一次。`/cr set forecast off` 可关闭估算提醒。个人额度和续期时间请在产品的账户页面查看。

## 快速开始

1. 在 AstrBot WebUI 使用 [GitHub 仓库地址](https://github.com/kasumi-ppp/astrbot_plugin_codex_reset)安装插件并启用。手动安装时，将仓库放到 `data/plugins/astrbot_plugin_codex_reset`，在 AstrBot 的 Python 环境中安装插件的 `requirements.txt`。
2. 在插件配置中开启采集总开关，再开启需要的产品。Codex 之外的产品默认关闭。
3. AstrBot 管理员在目标 QQ 群执行以下命令，以 Claude 为例。将 `claude` 换成表里的订阅名即可切换产品。

   ```text
   /cr subscribe claude
   /cr status
   /cr test claude
   ```

已有群可在 WebUI 的「群级订阅与通知配置」选择任意产品，保存后同步到群订阅。全部关闭会暂停该群推送并保留卡片。新群先执行 `/cr subscribe` 建立卡片。

群内 `/cr` 和 `/cr status` 只列出本群已订阅的产品，每个产品显示最近一次已确认重置（北京时间）或「暂无确认记录」。`/cr status detail` 可查看采集和最新事件，私聊状态列出全部产品。`/cr test antigravity-gemini watch` 可试发预警。测试消息用于确认 QQ 发送通道。连续测试请间隔至少 60 秒。

| 常用命令 | 用途 |
|---|---|
| `/cr subscribe [产品]` | 订阅该产品，省略时订阅 Codex |
| `/cr unsubscribe [产品]` | 退订该产品，省略时取消本群所有订阅 |
| `/cr forecast`、`/cr history` | 查看 Codex 预测或重置历史 |
| `/cr help` | 查看完整命令。若有指令冲突，用 `/codexreset` 代替 `/cr` |

## 重置状态图片

开启插件全局「每日图片报告」并在目标群执行 `/cr set daily on` 后，定时报告会按该群订阅的产品生成，每个产品一行，显示最近一次已确认重置（北京时间）或「暂无确认记录」，并保留来源标注。默认发送时间为北京时间 09:00，原有重试机制不变。`/cr report` 可立即查看本群报告；`/cr test image` 发送使用本群当前订阅数据的测试图片，图片标题为「最近确认重置（测试）」。未订阅群执行报告或测试图片命令时会收到简短提示，不发送 PNG。

## 数据来源与帮助

- Codex 读取 [Codex Reset](https://codex-reset.com/)。Claude 读取 [Claude Reset](https://claudereset.org/developers/)，其中概率来自 WhenReset 的估算。
- Grok Bot、Grok Build 和 Google Gemini 读取 [QuotaResets](https://quotaresets.com/api/v1/events.json)。Antigravity Gemini 读取 [AGY Resets](https://agyresets.com/api/docs)，覆盖 Antigravity 内的额度公告。
- 这些站点整理公开消息，覆盖率取决于上游收录。事件通知附数据来源与原帖链接，概率和预警均为未确认信号。
- Docker 部署请持久化 AstrBot 的 `data/` 目录，以保留订阅和去重记录。遇到 HTTP 403、采集失败或群内收不到消息，可按手册逐项检查。[服务器验收与排障](docs/ACCEPTANCE-RUNBOOK.md)
- [进阶用法](docs/usage-notes.md) · [版本记录](CHANGELOG.md) · [AGPL-3.0 许可证](LICENSE)
