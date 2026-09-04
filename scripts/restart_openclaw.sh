#!/usr/bin/env bash
# 安全重启 / 自检 OpenClaw gateway
#
# 背景（2026-08-31 踩坑）：gateway 有 crash-loop breaker —— 300000ms 内 3 次
# unclean boot 即跳闸，之后 openclaw-weixin 通道被禁止自动启动，表现为
# 「微信发几条后就静默中断」。裸 `kill` + 重启极易打满该计数器。
#
# 本脚本的防护：
#   1. 重启前自检：距上次启动不足 breaker 窗口（默认 300s）则拒绝重启，改为走通道兜底；
#   2. 优雅停止：SIGTERM → 等待退出 → 才 SIGKILL，减少 unclean boot；
#   3. 启动后轮询就绪；若通道 running=false，自动用 RPC `channels.start` 拉起
#      （CLI 没有 `channels start` 子命令，必须走 `gateway call`）；
#   4. 输出自检报告：gateway / 通道 / contextToken 新鲜度 / DSA 服务。
#
# 用法：
#   ./scripts/restart_openclaw.sh            # 安全重启（自检 + 兜底自愈）
#   ./scripts/restart_openclaw.sh --check    # 只自检（发现通道未运行会自动兜底拉起）
#   ./scripts/restart_openclaw.sh --force    # 忽略「间隔不足」保护，强制重启
#   ./scripts/restart_openclaw.sh --probe    # 结束后发一条微信测试消息

set -u

CHANNEL="${OPENCLAW_CHANNEL:-openclaw-weixin}"
PORT="${OPENCLAW_GATEWAY_PORT:-18789}"
BREAKER_WINDOW="${OPENCLAW_BREAKER_WINDOW:-300}"
READY_TIMEOUT="${OPENCLAW_READY_TIMEOUT:-90}"
STATE_FILE="/tmp/openclaw_last_boot_ts"
ACCOUNTS_DIR="$HOME/.openclaw/openclaw-weixin/accounts"
DSA_URL="${DSA_BASE_URL:-http://127.0.0.1:8000}"
# 2026-08-31：gateway 已由 launchd（KeepAlive）托管。此时**禁止**手动 nohup 启动，
# 否则会与 launchd 实例抢 18789 端口 → EADDRINUSE → unclean boot → crash-loop breaker。
LAUNCHD_LABEL="com.openclaw.gateway"

launchd_managed() {
  launchctl list 2>/dev/null | grep -q "$LAUNCHD_LABEL"
}

MODE="restart"
FORCE=0
PROBE=0

usage() {
  sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'
}

for arg in "$@"; do
  case "$arg" in
    --check) MODE="check" ;;
    --force) FORCE=1 ;;
    --probe) PROBE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $arg（--check / --force / --probe）"; exit 2 ;;
  esac
done

# 定位 openclaw 可执行文件
OC="$(command -v openclaw || true)"
if [ -z "$OC" ]; then
  OC="$(ls -d "$HOME"/.npm/_npx/*/node_modules/.bin/openclaw 2>/dev/null | head -1 || true)"
fi
if [ -z "$OC" ]; then
  echo "❌ 未找到 openclaw 可执行文件"; exit 3
fi

gw_pid() { lsof -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1; }

etime_to_sec() {
  local e="${1:-}" d=0 h=0 m=0 s=0 a b c
  [ -z "$e" ] && { echo 0; return; }
  e="$(echo "$e" | tr -d ' ')"
  if [[ "$e" == *-* ]]; then d="${e%%-*}"; e="${e#*-}"; fi
  IFS=: read -r a b c <<< "$e"
  if [ -n "${c:-}" ]; then h=$a; m=$b; s=$c
  elif [ -n "${b:-}" ]; then m=$a; s=$b
  else s=$a; fi
  echo $(( 10#${d:-0}*86400 + 10#${h:-0}*3600 + 10#${m:-0}*60 + 10#${s:-0} ))
}

# 输出: "<running>\\t<lastError>"，gateway 未就绪时 running=unknown
channel_state() {
  "$OC" channels status --json 2>/dev/null | python3 -c '
import json, sys
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except Exception:
    print("unknown\t<gateway 未就绪>")
    raise SystemExit(0)
ch = sys.argv[1]
acc = (d.get("channelAccounts", {}).get(ch) or [{}])[0]
print("%s\t%s" % (str(acc.get("running")).lower(), acc.get("lastError") or ""))
' "$CHANNEL"
}

start_channel() {
  echo "   → 调用 RPC channels.start 兜底拉起通道…"
  local out
  out="$("$OC" gateway call channels.start --params "{\"channel\":\"$CHANNEL\"}" --json 2>&1 | head -5)"
  echo "   → 返回: $(echo "$out" | tr -d '\n' | cut -c1-200)"
}

probe_wechat() {
  local account target
  account="$("$OC" channels status --json 2>/dev/null | python3 -c '
import json,sys
try:
    print(json.load(sys.stdin).get("channelDefaultAccountId",{}).get("'"$CHANNEL"'",""))
except Exception:
    print("")')"
  [ -z "$account" ] && { echo "   ⚠️ 取不到 accountId，跳过探测"; return; }
  target="$(python3 -c '
import glob, json, os, sys
files = sorted(glob.glob(os.path.expanduser("~/.openclaw/openclaw-weixin/accounts/*.json")))
for f in files:
    try:
        d = json.load(open(f))
    except Exception:
        continue
    if d.get("userId"):
        print(d["userId"]); break
')"
  [ -z "$target" ] && { echo "   ⚠️ 取不到接收人 userId，跳过探测"; return; }
  echo "   → 发送测试消息到 $target …"
  "$OC" message send --channel "$CHANNEL" --account "$account" --target "$target" \
    --message "✅ OpenClaw 自检探测：微信推送链路正常" --json 2>&1 \
    | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    print("   → deliveryStatus:", (d.get("payload") or {}).get("deliveryStatus"))
except Exception:
    print("   → 发送结果解析失败")
'
}

report() {
  echo ""
  echo "======== 自检报告 ========"
  local pid up st running err
  pid="$(gw_pid)"
  if [ -n "$pid" ]; then
    up="$(ps -o etime= -p "$pid" 2>/dev/null | tr -d ' ')"
    mgr="手动 nohup"
    launchd_managed && mgr="launchd (KeepAlive)"
    echo "gateway     : PID $pid, 已运行 ${up:-未知}, 管理方式: $mgr"
    local procs
    procs="$(ps aux | grep -c '[o]penclaw-gateway')"
    if [ "${procs:-0}" -gt 1 ]; then
      echo "             ⚠️ 检测到 ${procs} 个 openclaw-gateway 进程 — 多实例会抢 $PORT 端口"
      echo "                （EADDRINUSE → unclean boot → crash-loop breaker）。请只保留 launchd 那个。"
    fi
  else
    echo "gateway     : ❌ 未运行（端口 $PORT 无监听）"
  fi

  if [ -n "$pid" ]; then
    st="$(channel_state)"
    running="${st%%$'\t'*}"
    err="${st#*$'\t'}"
    if [ "$running" = "true" ]; then
      echo "通道 $CHANNEL : ✅ running"
    else
      echo "通道 $CHANNEL : ❌ 未运行（running=$running）"
      [ -n "$err" ] && echo "             lastError: $err"
    fi
  fi

  local tok now age
  tok="$(ls -t "$ACCOUNTS_DIR"/*.context-tokens.json 2>/dev/null | head -1)"
  if [ -n "$tok" ]; then
    now="$(date +%s)"
    age=$(( (now - $(stat -f %m "$tok")) / 60 ))
    if [ "$age" -le 60 ]; then
      echo "contextToken: ✅ $(basename "$tok")，${age} 分钟前刷新"
    else
      echo "contextToken: ⚠️ $(basename "$tok")，已 ${age} 分钟未刷新（需在微信给 bot 发一条消息）"
    fi
  else
    echo "contextToken: ⚠️ 未找到 context-tokens.json"
  fi

  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$DSA_URL/api/v1/bot/commands" 2>/dev/null)"
  echo "DSA 服务    : $([ "$code" = "200" ] && echo "✅ $DSA_URL 正常" || echo "❌ 无响应 (HTTP ${code:-none})")"
  echo "=========================="
}

# ---------------- 仅自检 ----------------
if [ "$MODE" = "check" ]; then
  echo "[check] 只做自检，不重启 gateway"
  pid="$(gw_pid)"
  if [ -z "$pid" ]; then
    echo "⚠️ gateway 未运行。请执行 $0 重启（注意 breaker 窗口）"
    report
    exit 1
  fi
  st="$(channel_state)"
  running="${st%%$'\t'*}"
  if [ "$running" != "true" ]; then
    echo "⚠️ 通道未运行，尝试兜底拉起（不重启 gateway，避免触发 breaker）…"
    start_channel
    sleep 3
  fi
  [ "$PROBE" = "1" ] && probe_wechat
  report
  exit 0
fi

# ---------------- 重启 ----------------
pid="$(gw_pid)"
if [ -n "$pid" ]; then
  up="$(ps -o etime= -p "$pid" 2>/dev/null | tr -d ' ')"
  sec="$(etime_to_sec "$up")"
  if [ "$sec" -lt "$BREAKER_WINDOW" ] && [ "$FORCE" != "1" ]; then
    echo "⛔ 拒绝重启：当前 gateway 仅运行 ${sec}s < breaker 窗口 ${BREAKER_WINDOW}s。"
    echo "   连续 unclean boot 会触发 crash-loop breaker（3 次/5 分钟）导致通道被禁自启。"
    echo "   建议：改用 --check 走通道兜底；确需重启请等满 ${BREAKER_WINDOW}s 或加 --force。"
    exit 1
  fi
  if launchd_managed; then
  echo "[1/4] 通过 launchd 重启 gateway（KeepAlive 托管，禁止手动 nohup）…"
  launchctl kickstart -k "gui/$(id -u)/$LAUNCHD_LABEL" 2>&1 | head -3
  echo "[2/4] 等待 launchd 拉起…"
  echo "[3/4] 等待 gateway 就绪（最多 ${READY_TIMEOUT}s）…"
  ready=0
  for i in $(seq 1 "$READY_TIMEOUT"); do
    st="$(channel_state)"
    if [ "${st%%$'\t'*}" != "unknown" ]; then ready=1; break; fi
    sleep 1
  done
  [ "$ready" != "1" ] && { echo "❌ gateway 超时未就绪，查看 ~/Library/Logs/openclaw-gateway.err.log"; report; exit 1; }
  echo "   → 就绪（${i}s）"
  echo "[4/4] 校验通道状态…"
  st="$(channel_state)"
  running="${st%%$'\t'*}"
  err="${st#*$'\t'}"
  if [ "$running" != "true" ]; then
    echo "   ⚠️ 通道未自动启动（running=$running）"
    echo "   → lastError: $err"
    start_channel
    sleep 3
  fi
  [ "$PROBE" = "1" ] && probe_wechat
  report
  exit 0
fi

echo "[1/4] 优雅停止 gateway (PID $pid, SIGTERM)…"
  kill -TERM "$pid" 2>/dev/null
  for _ in $(seq 1 15); do
    sleep 1
    [ -z "$(gw_pid)" ] && break
  done
  if [ -n "$(gw_pid)" ]; then
    echo "   → 15s 未退出，SIGKILL"
    kill -KILL "$pid" 2>/dev/null
    sleep 2
  fi
else
  echo "[1/4] 未检测到运行中的 gateway，跳过停止"
fi

echo "[2/4] 启动 gateway (--force --bind loopback)…"
nohup "$OC" gateway run --force --bind loopback >/tmp/openclaw_gateway.log 2>&1 &
disown
date +%s > "$STATE_FILE"

echo "[3/4] 等待 gateway 就绪（最多 ${READY_TIMEOUT}s）…"
ready=0
for i in $(seq 1 "$READY_TIMEOUT"); do
  st="$(channel_state)"
  if [ "${st%%$'\t'*}" != "unknown" ]; then ready=1; break; fi
  sleep 1
done
[ "$ready" != "1" ] && { echo "❌ gateway 超时未就绪，查看 /tmp/openclaw_gateway.log"; report; exit 1; }
echo "   → 就绪（${i}s）"

echo "[4/4] 校验通道状态…"
st="$(channel_state)"
running="${st%%$'\t'*}"
err="${st#*$'\t'}"
if [ "$running" != "true" ]; then
  echo "   ⚠️ 通道未自动启动（running=$running）"
  echo "   → lastError: $err"
  start_channel
  sleep 3
fi

[ "$PROBE" = "1" ] && probe_wechat
report
