#!/usr/bin/env bash
# DSA 微信指令触发脚本（仅本地调用，不修改 DSA 仓库）
# 用法:
#   dsa_cmd.sh market            # 大盘复盘 / 指数分析
#   dsa_cmd.sh stock 600519      # 个股分析（支持逗号分隔多个代码）
#   dsa_cmd.sh stock 600519,300750
#   dsa_cmd.sh stock 贵州茅台     # 也支持股票名称（自动解析为代码）
#   dsa_cmd.sh stock 贵州茅台,宁德时代
#
# 说明: 脚本在后台启动 DSA 分析任务。分析结束后，DSA 会复用已配置的
# openclaw_wechat 渠道，把报告图片自动推送回微信会话。脚本立即返回，
# 便于 openclaw agent 即时给用户确认消息而不会阻塞。

set -u

# 清理 PATH：移除损坏的旧 node（/usr/local/Cellar/node/11.14.0_1/bin 依赖缺失的 icu4c 64），
# 并优先使用可用的 nvm node，避免 md2img 调用到崩溃的 node 引擎。
GOOD_NODE_DIR="/Users/sunchang/.nvm/versions/node/v24.15.0/bin"
NEW_PATH=""
IFS=':' read -ra _path_parts <<< "$PATH"
for _p in "${_path_parts[@]}"; do
  case "$_p" in
    /usr/local/Cellar/node/*/bin) continue ;;
  esac
  [ -n "$_p" ] && NEW_PATH="${NEW_PATH:+$NEW_PATH:}$_p"
done
if [ -d "$GOOD_NODE_DIR" ]; then
  NEW_PATH="${GOOD_NODE_DIR}${NEW_PATH:+:$NEW_PATH}"
fi
export PATH="$NEW_PATH"

DSA_DIR="/Users/sunchang/daily_stock_analysis-main"
LOG_PREFIX="/tmp/dsa_cmd"

# 优先用仓库约定解释器 python，回退 python3
PY=$(command -v python || command -v python3 || true)
if [ -z "$PY" ]; then
  echo "未找到 python / python3"; exit 3
fi

run_bg() {
  local label="$1"
  shift
  local log="$LOG_PREFIX.$label.log"
  # 微信触发需要即时分析并推送报告，覆盖 .env 中 RUN_IMMEDIATELY=false 的默认值
  nohup env RUN_IMMEDIATELY=true "$PY" -u "$DSA_DIR/main.py" "$@" >"$log" 2>&1 &
  echo "started pid=$! log=$log"
}

case "${1:-}" in
  market|review|大盘|复盘|指数|market-review)
    run_bg "market" "--market-review"
    echo "已启动大盘复盘分析，报告图片稍后由 DSA 经 openclaw_wechat 推送。"
    ;;
  stock)
    RAW="${2:-}"
    if [ -z "$RAW" ]; then
      echo "用法: dsa_cmd.sh stock <代码或名称[,...]>";
      exit 2
    fi
    # 若输入含中文名称，先用 DSA 既有 resolver 解析为代码；反之原样传给 --stocks
    CODE=$(cd "$DSA_DIR" && "$PY" -c '
import sys
from src.services.name_to_code_resolver import resolve_name_to_code
raw = sys.argv[1]
out = []
for part in raw.split(","):
    part = part.strip()
    if not part:
        continue
    if any("\u4e00" <= ch <= "\u9fff" for ch in part):
        code = resolve_name_to_code(part)
        out.append(code if code else part)
    else:
        out.append(part)
print(",".join(out))
' "$RAW" 2>/dev/null)
    if [ -z "$CODE" ]; then
      CODE="$RAW"
    fi
    # --stocks 会覆盖默认自选股列表，仅分析指定代码（或解析后的代码）
    run_bg "stock_${RAW}" "--stocks" "$CODE"
    echo "已启动个股分析($RAW -> $CODE)，报告图片稍后由 DSA 经 openclaw_wechat 推送。"
    ;;
  screen|sideways|横盘|横盘选股|横盘突破|consolidation)
    REFRESH=""
    for arg in "$@"; do
      case "$arg" in
        --refresh|-r) REFRESH="--refresh" ;;
      esac
    done
    # 先看缓存是否已有今日结果
    HIT=$("$PY" -c "
import sys, json
sys.path.insert(0, '$DSA_DIR')
from src.services.screening_service import read_alphasift_screen_cache
cache = read_alphasift_screen_cache('sideways_breakout')
if cache and cache.get('date') == '$(date +%Y-%m-%d)':
    print('HIT:' + json.dumps(cache, ensure_ascii=False, default=str))
else:
    print('MISS')
" 2>/dev/null)
    if [ -n "$REFRESH" ] || echo "$HIT" | grep -q '^MISS'; then
      run_bg "screen" "--screen" "sideways_breakout" "$REFRESH"
      echo "🔄 横盘突破选股已启动，正在扫描全市场 A 股（约 2–5 分钟）。完成后通过微信推送结果。"
    else
      # 提取缓存中的候选股列表，直接推送
      REPORT=$(echo "$HIT" | sed 's/^HIT://')
      echo "$REPORT" | "$PY" -c "
import sys, json
from datetime import datetime
data = json.load(sys.stdin)
candidates = data.get('candidates', [])
date = data.get('date', '')
lines = []
lines.append(f'📊 **横盘突破选股结果 — {date}**')
lines.append('')
lines.append(f'共筛选出 **{len(candidates)}** 只候选股：')
lines.append('')
for i, c in enumerate(candidates, 1):
    code = c.get('code', '?')
    name = c.get('name', '?')
    pct = c.get('change_pct', 0)
    pct_str = f\"{'+' if pct >= 0 else ''}{pct:.2f}%\"
    price = c.get('price', '?')
    quote = c.get('dsa_context', {}).get('quote', {})
    vol = quote.get('volume_ratio', '?')
    vol_str = f'量比 {vol:.2f}' if isinstance(vol, (int, float)) else ''
    lines.append(f'{i}. **{name} ({code})** ¥{price} {pct_str}  {vol_str}')
lines.append('')
lines.append('💡 回复 /analyze <code> 查看个股深度分析')
print('\n'.join(lines))
" > "$LOG_PREFIX.screen_result.txt" 2>/dev/null
      echo "✅ 今日已选股完毕，结果如下："
      cat "$LOG_PREFIX.screen_result.txt" 2>/dev/null || echo "（结果文件生成失败）"
    fi
    ;;
  *)
    echo "未知指令: ${1:-空}（支持 market / stock <代码> / screen）";
    exit 2
    ;;
esac
