# OpenClaw 接入配置唯一真源

本目录是 DSA 微信接入 OpenClaw 的**配置唯一真源**（version-controlled）。
`~/.openclaw/` 下的运行时文件是这里的镜像/部署副本，任何丢失/覆盖都可通过
`scripts/sync_openclaw.sh` 从本目录一键恢复。

> ⚠️ **不要在本目录之外手写维护同义配置**（见仓库根 `AGENTS.md` 的 AI 协作资产治理原则）。
> 改接入逻辑请只改本目录，再运行 `scripts/sync_openclaw.sh` 同步到 `~/.openclaw/`。

## 文件说明

| 文件 | 运行时部署目标 | 作用 |
|------|---------------|------|
| `workspace/AGENTS.md` | `~/.openclaw/workspace-dsa-notify/AGENTS.md` | dsa-notify agent 操作说明：微信里「大盘/个股」触发 `dsa_cmd.sh` |
| `skills/daily-stock-analysis/SKILL.md` | `~/.openclaw/skills/daily-stock-analysis/SKILL.md` | OpenClaw skill：调 REST API / 指令菜单 |
| `agents/dsa_cmd.sh` | `~/.openclaw/agents/dsa-notify/dsa_cmd.sh` | 微信触发 DSA 分析的后台脚本 |
| `openclaw.json` | `~/.openclaw/openclaw.json` | OpenClaw 全局配置（含 `skills.entries`、agent/hook） |

## 为什么命令信息会“丢”

- **DSA 命令本身（/analyze /market /menu 等 11 条）永不丢**：硬编码在
  `bot/commands/*.py`，由 `GET /api/v1/bot/commands` 实时生成，与 OpenClaw 无关。
- **真正会丢的是 OpenClaw 接入配置**：`openclaw.json` 会在 gateway 更新 / onboarding /
  误操作时被重写，手工注入的 `skills.entries`、`agents`、`hooks` 段可能被合并覆盖或丢失；
  `AGENTS.md` / `SKILL.md` / `dsa_cmd.sh` 之前没有任何备份。本目录 + git 解决此问题。

## 恢复步骤

```bash
# 一键把真源同步/恢复到 ~/.openclaw/（幂等，覆盖运行时副本）
./scripts/sync_openclaw.sh
```

## 注意

- `DSA_BASE_URL` 在本仓库约定为 `http://127.0.0.1:8000`；如你的部署地址不同，
  请改 `openclaw.json` 模板后重新 `./scripts/sync_openclaw.sh`。
- `openclaw.json` 模板只含 **DSA 相关** 的接入段；同步脚本会做合并，不会丢弃
  `~/.openclaw/openclaw.json` 中与 DSA 无关的现有配置（详见脚本内注释）。
