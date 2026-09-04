#!/usr/bin/env bash
# 把 integrations/openclaw/ 下的「OpenClaw 接入配置唯一真源」同步/恢复到 ~/.openclaw/。
#
# 设计目标：幂等、只增不改 DSA 无关配置、不覆盖 hooks token 等密钥。
# 用法：
#   ./scripts/sync_openclaw.sh            # 同步/恢复全部
#   ./scripts/sync_openclaw.sh --dry-run  # 仅打印将要执行的操作，不落地
#
# 说明：openclaw.json 会被 OpenClaw 在 gateway 更新/onboarding 时重写，手工注入的
# skills.entries / agents / hooks 段可能被覆盖丢失。本脚本把这些 DSA 相关段
# 「合并」进现有 ~/.openclaw/openclaw.json：不存在的键补上，已存在的保留（尤其
# hooks.token、channels 等密钥/账号配置绝不覆盖）。

set -u

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO_DIR/integrations/openclaw"
DEST="$HOME/.openclaw"

AGENT_ID="dsa-notify"
AGENT_NAME="DSA Notify"
AGENT_WORKSPACE="$DEST/workspace-dsa-notify"
AGENT_MODEL_REF="minimax/MiniMax-M2.7"  # OpenClaw providers 里实际存在的 MiniMax 免费模型（tokenplan）；M3 不存在，绑 M3 会导致 fallback 到不存在的 gpt-5.2 而报错。
SKILL_NAME="daily-stock-analysis"
DSA_BASE_URL="http://127.0.0.1:8000"

DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

log()  { printf '[sync] %s\n' "$*"; }
logd() { [ "$DRY" = 1 ] && printf '[dry-run] %s\n' "$*" || log "$*"; }
do_cp() { # src dst
  if [ "$DRY" = 1 ]; then logd "copy $1 -> $2"; return; fi
  mkdir -p "$(dirname "$2")"
  cp "$1" "$2"
  log "copy $1 -> $2"
}

[ -f "$SRC/workspace/AGENTS.md" ] || { echo "缺少真源: $SRC/workspace/AGENTS.md"; exit 1; }
[ -f "$SRC/skills/$SKILL_NAME/SKILL.md" ] || { echo "缺少真源: $SRC/skills/$SKILL_NAME/SKILL.md"; exit 1; }
[ -f "$SRC/agents/dsa_cmd.sh" ] || { echo "缺少真源: $SRC/agents/dsa_cmd.sh"; exit 1; }

# ---------- 1) 纯文件复制（AGENTS.md / SKILL.md / dsa_cmd.sh） ----------
do_cp "$SRC/workspace/AGENTS.md"        "$DEST/workspace-dsa-notify/AGENTS.md"
do_cp "$SRC/skills/$SKILL_NAME/SKILL.md" "$DEST/skills/$SKILL_NAME/SKILL.md"
do_cp "$SRC/agents/dsa_cmd.sh"          "$DEST/agents/dsa-notify/dsa_cmd.sh"
if [ "$DRY" != 1 ]; then chmod +x "$DEST/agents/dsa-notify/dsa_cmd.sh"; fi

# ---------- 2) 合并 openclaw.json（只补不覆盖） ----------
CFG="$DEST/openclaw.json"
[ -f "$CFG" ] || { echo "缺少现有配置: $CFG（首次需先手动运行 openclaw onboarding）"; exit 1; }

PY_BODY=$(cat <<'PYEOF'
import json, os, sys
cfg_path, agent_id, agent_name, agent_ws, model_ref, skill_name, dsa_base = (
    sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6], sys.argv[7]
)
with open(cfg_path) as f:
    d = json.load(f)

changed = []

# --- agents.list: 确保 dsa-notify 存在 ---
agents = d.setdefault("agents", {})
lst = agents.setdefault("list", [])
found = next((a for a in lst if a.get("id") == agent_id), None)
if found is None:
    lst.append({
        "id": agent_id,
        "name": agent_name,
        "default": True,
        "contextTokens": 128000,
        "workspace": agent_ws,
        "tools": {"profile": "coding", "deny": ["web_search", "web_fetch", "browser"]},
    })
    changed.append("agents.list[dsa-notify]")
else:
    # 只补关键字段，不覆盖现有
    found.setdefault("name", agent_name)
    found.setdefault("workspace", agent_ws)
    found.setdefault("contextTokens", 128000)
    # 模型配置必须强制同步真源：错误模型会让短指令(dsa)随机回emoji或触发fallback提示
    found["model"] = {"primary": model_ref, "fallbacks": []}
    tools = found.setdefault("tools", {})
    tools.setdefault("profile", "coding")
    deny = tools.setdefault("deny", ["web_search", "web_fetch", "browser"])
    for denied in ("web_search", "web_fetch", "browser"):
        if denied not in deny:
            deny.append(denied)
    changed.append("agents.list[dsa-notify](merged)")

# --- agents.defaults.model: 强制同步为真源模型 ---
# 注意：模型配置应来自 AGENT_MODEL_REF 单一真源；此前错误地把"MiniMax-M3 回 emoji"
# 当真因并擅自换成 deepseek，实测 emoji 非 LLM 输出（见 2026-08-31 记录）。
# OpenClaw 里实际存在的 MiniMax 模型是 M2.7，不是 M3；绑不存在的模型会 fallback 到 defaults.models 里的旧模型导致报错。
defaults = agents.setdefault("defaults", {})
model = defaults.setdefault("model", {})
model["primary"] = model_ref
model["fallbacks"] = []
# 清掉可能残留的旧 defaults.models，避免它覆盖 agent.model 的单一真源选择。
defaults.pop("models", None)

# --- hooks.mappings: 确保 dsa-notify 映射存在（保留现有 hooks.token）---
hooks = d.setdefault("hooks", {})
hooks.setdefault("enabled", True)
hooks.setdefault("path", "/hooks")
hooks.setdefault("defaultSessionKey", "hook:ingress")
mappings = hooks.setdefault("mappings", [])
mfound = next((m for m in mappings if m.get("id") == agent_id), None)
if mfound is None:
    mappings.append({
        "id": agent_id,
        "match": {"path": "agent"},
        "action": "agent",
        "agentId": agent_id,
        "wakeMode": "now",
        "name": "DSA 通知",
        "sessionKey": "hook:agent",
        "messageTemplate": "{{message}}",
    })
    changed.append("hooks.mappings[dsa-notify]")
else:
    mfound.setdefault("agentId", agent_id)
    mfound.setdefault("action", "agent")
    changed.append("hooks.mappings[dsa-notify](merged)")

# --- skills.entries: 确保 daily-stock-analysis 存在 ---
skills = d.setdefault("skills", {})
entries = skills.setdefault("entries", {})
if skill_name not in entries:
    entries[skill_name] = {"enabled": True, "env": {"DSA_BASE_URL": dsa_base}}
    changed.append(f"skills.entries[{skill_name}]")
else:
    entries[skill_name].setdefault("enabled", True)
    env = entries[skill_name].setdefault("env", {})
    env.setdefault("DSA_BASE_URL", dsa_base)
    changed.append(f"skills.entries[{skill_name}](merged)")

with open(cfg_path, "w") as f:
    json.dump(d, f, ensure_ascii=False, indent=2)
    f.write("\n")
print(";".join(changed) if changed else "NOCHANGE")
PYEOF
)

if [ "$DRY" = 1 ]; then
  logd "merge DSA 相关段进 $CFG（只补不覆盖，保留 hooks.token 等密钥）"
else
  RES=$(python3 -c "$PY_BODY" "$CFG" "$AGENT_ID" "$AGENT_NAME" "$AGENT_WORKSPACE" "$AGENT_MODEL_REF" "$SKILL_NAME" "$DSA_BASE_URL" 2>&1)
  if [ "$RES" = "NOCHANGE" ]; then
    log "openclaw.json 已包含全部 DSA 段，无需改动"
  else
    log "openclaw.json 已合并: ${RES}"
  fi
fi

log "完成。如需让新配置生效，请重启 gateway: npx openclaw gateway restart"
