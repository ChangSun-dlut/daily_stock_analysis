"""Quick backtest: bottom_accumulation strategy vs 利欧股份 (002131) on 2026-07-28."""
import sys
import os

os.environ.setdefault("LOGURU_LEVEL", "WARNING")
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime, date
import pandas as pd
import numpy as np

from data_provider.base import DataFetcherManager
from src.services.screening.daily import compute_daily_features
from src.services.screening.strategy import load_strategy
from src.services.screening.filter import apply_hard_filters
from src.services.screening.scorer import compute_screen_scores
from pathlib import Path


def main():
    code = "002131"
    name = "利欧股份"
    target_date = date(2026, 7, 28)
    target_str = target_date.isoformat()

    print(f"=" * 70)
    print(f"回测: {name} ({code}) — {target_str}")
    print(f"策略: bottom_accumulation (底部吸筹反转)")
    print(f"=" * 70)

    # ---------- Step 1: Load strategy ----------
    strat = load_strategy(Path("strategies/bottom_accumulation.yaml"))
    hf = strat.screening.hard_filters
    hf.exclude_st = False
    sp = strat.screening.scoring_profile
    fw = strat.screening.factor_weights
    print(f"\n📋 策略加载: {strat.display_name} v{strat.version}")
    print(f"   bottom_accumulation_bypass = {hf.bottom_accumulation_bypass}")
    print(f"   bottom_accumulation_quality 权重 = {fw.get('bottom_accumulation_quality', 'N/A')}")
    print(f"   change_60d_max = {hf.change_60d_max}")

    # ---------- Step 2: Fetch daily history ----------
    print(f"\n🔍 获取 {code} 日线数据 (60+ 交易日)...")
    mgr = DataFetcherManager()
    df, source = mgr.get_daily_data(stock_code=code, days=120)
    if df is None or df.empty:
        print("❌ 无法获取日线数据")
        return
    print(f"   数据源: {source}, 共 {len(df)} 条")

    # Strip timezone from date column if present
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"]).dt.date

    # Filter up to target_date
    if "date" in df.columns:
        df = df[df["date"] <= target_date]
        print(f"   截取到 {target_str}: 剩余 {len(df)} 条")

    if len(df) < 60:
        print(f"❌ 数据不足 (需要至少60条, 实际{len(df)}条)")
        return

    # Print key dates
    print(f"   日期范围: {min(df['date'])} ~ {max(df['date'])}")

    # ---------- Step 3: Compute daily features ----------
    print(f"\n📊 计算 daily features...")
    features = compute_daily_features(df)

    print(f"   数据点数: {features['daily_data_points']}")
    print(f"   60日涨跌: {features.get('change_60d', 'N/A')}%")
    print(f"   20日涨跌: {features.get('change_20d', 'N/A')}%")
    print(f"   10日涨跌: {features.get('change_10d', 'N/A')}%")
    print(f"   RSI(14): {features.get('rsi14', 'N/A')}")
    print(f"   MACD 状态: {features.get('macd_status', 'N/A')}")
    print(f"   MA 多头: {features.get('ma_bullish', 'N/A')}")
    print(f"   站上 MA20: {features.get('price_above_ma20', 'N/A')}")
    print(f"   20日振幅: {features.get('range_20d_pct', 'N/A')}%")
    print(f"   20日波动率(年化): {features.get('volatility_20d_pct', 'N/A')}%")
    print(f"   20日横盘天数: {features.get('consolidation_days_20d', 'N/A')}")
    print(f"   量比20日: {features.get('volume_ratio_20d', 'N/A')}")
    print(f"   5日量扩张: {features.get('volume_expand_5d', 'N/A')}")
    print(f"   1日量扩张: {features.get('volume_expand_1d', 'N/A')}")
    print(f"   突破20日高点%: {features.get('breakout_20d_pct', 'N/A')}%")

    # ---------- Step 4: Bottom accumulation specifics ----------
    print(f"\n🔥 底部吸筹信号检查:")
    print(f"   RSI近期曾超卖(<35): {features.get('rsi_oversold_20d', 'N/A')}")
    print(f"   RSI恢复幅度: {features.get('rsi_recovery_pct', 'N/A')}")
    print(f"   MACD 底部金叉: {features.get('macd_bottom_cross', 'N/A')}")
    print(f"   MA5 拐头幅度: {features.get('ma5_turn_up_pct', 'N/A')}%")
    print(f"   距60日低点: {features.get('price_vs_60d_low_pct', 'N/A')}%")
    print(f"   ★ 底部吸筹信号达标: {features.get('bottom_accumulation_signal', 'N/A')} ★")

    # ---------- Step 5: Fetch realtime quote snapshot ----------
    print(f"\n📡 获取实时行情快照...")
    quote = mgr.get_realtime_quote(code)
    if quote is None:
        print("   ⚠️ 实时行情获取失败，使用日线数据估算快照字段")
        # Fallback: compute snapshot-fields from daily data
        last_close = float(pd.to_numeric(df["close"], errors="coerce").iloc[-1])
        amount_val = float(pd.to_numeric(df.get("amount", pd.Series([0])), errors="coerce").iloc[-1])
        turnover = 5.0  # estimate
        volume_ratio = float(features.get("volume_ratio_20d", 1.0))
        change_pct = 0.0
    else:
        last_close = float(getattr(quote, "price", None) or getattr(quote, "latest_price", 0) or 0)
        amount_val = float(getattr(quote, "amount", 0) or 0)
        turnover = float(getattr(quote, "turnover_rate", None) or 5.0)
        volume_ratio = float(getattr(quote, "volume_ratio", None) or features.get("volume_ratio_20d", 1.0))
        change_pct = float(getattr(quote, "change_pct", 0) or 0)
        stale = getattr(quote, "stale_seconds", 0)
        print(f"   行情来源: 实时 (stale={stale}s)")
        print(f"   最新价: {last_close}, 涨跌幅: {change_pct}%")
        print(f"   量比: {volume_ratio}, 换手率: {turnover}%")
        print(f"   成交额: {amount_val/1e8:.2f}亿")

    # ---------- Step 6: Build snapshot DataFrame ----------
    snapshot_data = {
        "code": code,
        "name": name,
        "change_pct": change_pct,
        "amount": amount_val,
        "turnover_rate": turnover,
        "volume_ratio": volume_ratio,
        "change_60d": features.get("change_60d"),
        "change_20d": features.get("change_20d"),
        "change_10d": features.get("change_10d"),
        "range_20d_pct": features.get("range_20d_pct"),
        "volatility_20d_pct": features.get("volatility_20d_pct"),
        "consolidation_days_20d": features.get("consolidation_days_20d") or 0,
        "consolidation_days_60d": features.get("consolidation_days_60d") or 0,
        "breakout_20d_pct": features.get("breakout_20d_pct"),
        "ma_bullish": features.get("ma_bullish", False),
        "price_above_ma20": features.get("price_above_ma20", False),
        "macd_status": features.get("macd_status", "neutral"),
        "volume_expand_1d": features.get("volume_expand_1d"),
        "volume_expand_5d": features.get("volume_expand_5d"),
        "body_pct": features.get("body_pct"),
        "signal_score": features.get("signal_score", 50.0),
        "board_heat_score": 50.0,
        "daily_quality_score": features.get("daily_quality_score", 75.0),
        "daily_quality_flags": features.get("daily_quality_flags", ""),
        # Bottom accumulation fields
        "bottom_accumulation_signal": features.get("bottom_accumulation_signal", False),
        "bottom_accumulation_decline_60d": abs(features.get("change_60d") or 0),
        "bottom_accumulation_volume_expand": features.get("volume_expand_5d") or 1.0,
        "bottom_accumulation_rsi_recovery": features.get("rsi_recovery_pct") or 0.0,
        "bottom_accumulation_macd_cross": features.get("macd_bottom_cross", False),
        "bottom_accumulation_price_vs_60d_low": features.get("price_vs_60d_low_pct") or 0.0,
        "bottom_accumulation_ma5_turn_up": features.get("ma5_turn_up_pct") or 0.0,
        "bottom_accumulation_signal_count": (
            (1 if (features.get("change_60d") is not None and features.get("change_60d") <= -15) else 0) +
            (1 if bool(features.get("volume_expand_5d") or 0) >= 1.3 else 0) +
            (1 if bool(features.get("rsi_oversold_20d")) and (features.get("rsi_recovery_pct") or 0) >= 5 else 0) +
            (1 if features.get("macd_bottom_cross") else 0) +
            (1 if (features.get("price_vs_60d_low_pct") or 0) >= 3 else 0)
        ),
    }

    index_label = f"{code}.SZ"
    snapshot = pd.DataFrame([snapshot_data], index=[index_label])
    # Add all columns required by hard filters (even with -999/9999 "disabled" values)
    snapshot["price"] = last_close
    snapshot["volume_ratio_20d"] = features.get("volume_ratio_20d", 1.0)
    # Ensure all numeric columns are float
    for col in snapshot.columns:
        if col not in ("code", "name", "macd_status", "daily_quality_flags"):
            snapshot[col] = pd.to_numeric(snapshot[col], errors="coerce")

    # ---------- Step 7: Hard filter ----------
    print(f"\n🛡️ 硬过滤检查...")
    passed, filtered = apply_hard_filters(snapshot, hf)
    print(f"   通过: {'✅ 是' if not passed else '❌ 否 (被过滤)'}")
    if passed:
        print(f"   过滤详情: {len(filtered)} 条被过滤")
        for reason in filtered:
            print(f"     → {reason}")
    else:
        print(f"   bottom_accumulation_bypass 生效，股票通过硬过滤继续评分")

    # ---------- Step 8: Score ----------
    print(f"\n🏆 计算评分...")
    # apply_hard_filters returns (has_filtered, filtered_indices)
    # Use the original snapshot if it passed (has_filtered=False), else use filtered
    to_score = snapshot if not passed else filtered
    if to_score.empty:
        print("❌ 没有股票通过硬过滤，无法评分")
        return

    scored = compute_screen_scores(to_score, sp)
    if scored.empty:
        print("❌ 评分为空")
        return

    row = scored.iloc[0]
    print(f"\n{'='*70}")
    print(f"📊 评分结果")
    print(f"{'='*70}")
    for col in sorted(scored.columns):
        if "factor_" in col or "total" in col or "rank" in col or "accumulation" in col.lower():
            val = row.get(col)
            if val is not None and not (isinstance(val, float) and np.isnan(val)):
                if isinstance(val, float):
                    print(f"   {col}: {val:.2f}")
                else:
                    print(f"   {col}: {val}")

    # Fallback: print all factor scores
    factor_cols = [c for c in scored.columns if c.startswith("factor_")]
    if factor_cols:
        total = sum(float(row.get(c, 0) or 0) for c in factor_cols)
        print(f"\n   --- 因子得分汇总 ---")
        for c in factor_cols:
            val = row.get(c, 0)
            if val is None or (isinstance(val, float) and np.isnan(val)):
                val = 0
            print(f"   {c}: {float(val):.2f}")
        print(f"   总分: {float(total):.2f}")

    # ---------- Step 9: Conclusion ----------
    print(f"\n{'='*70}")
    print(f"📌 结论")
    print(f"{'='*70}")
    ba_signal = features.get("bottom_accumulation_signal", False)
    ba_score = scored.get("factor_bottom_accumulation_quality_score", None)
    if ba_score is not None and not (isinstance(ba_score, float) and np.isnan(ba_score.iloc[0])):
        ba_val = float(ba_score.iloc[0])
    else:
        ba_val = 0.0

    if ba_signal:
        print(f"✅ 底部吸筹信号触发! (5项中满足 {snapshot_data['bottom_accumulation_signal_count']} 项)")
        print(f"   bottom_accumulation_quality 评分: {ba_val:.2f}")
        if ba_val > 50:
            print(f"   🎯 该股票为底部吸筹反转强信号")
        else:
            print(f"   📈 该股票为底部吸筹反转中信号")
    else:
        print(f"❌ 底部吸筹信号未触发 (仅满足 {snapshot_data['bottom_accumulation_signal_count']}/5 项)")

    # Detail breakdown
    print(f"\n   信号明细:")
    decline = features.get("change_60d", 0) or 0
    print(f"   ① 60日跌幅 ≤ -15%: {'✅' if decline <= -15 else '❌'} ({decline:.2f}%)")
    ve = snapshot_data["bottom_accumulation_volume_expand"]
    print(f"   ② 量能扩张 ≥ 1.3: {'✅' if ve >= 1.3 else '❌'} ({ve:.2f}x)")
    rsio = features.get("rsi_oversold_20d", False)
    rsir = features.get("rsi_recovery_pct", 0) or 0
    print(f"   ③ RSI超卖恢复 ≥ 5%: {'✅' if rsio and rsir >= 5 else '❌'} (曾超卖={rsio}, 恢复={rsir:.1f}%)")
    mc = features.get("macd_bottom_cross", False)
    print(f"   ④ MACD底部金叉: {'✅' if mc else '❌'}")
    pvl = features.get("price_vs_60d_low_pct", 0) or 0
    print(f"   ⑤ 距60日低点 ≥ 3%: {'✅' if pvl >= 3 else '❌'} ({pvl:.2f}%)")
    print(f"   达标数: {snapshot_data['bottom_accumulation_signal_count']}/5 (需≥3)")

    print(f"\n✅ 回测完成!")


if __name__ == "__main__":
    main()
