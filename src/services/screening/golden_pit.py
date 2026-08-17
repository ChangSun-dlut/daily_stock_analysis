# -*- coding: utf-8 -*-
"""黄金坑形态识别器（Golden Pit pattern detector）。

专用形态识别，直接读取完整日线，识别
「底部横盘 → 首次放量拉升 → 黄金坑缩量回踩 → 温和反弹企稳」结构。

核心判据（按重要性）：
1. 距坑底反弹幅度——坑底温和反弹(0~12%) 为好，已起涨(>20%) 为差。
2. 坑底缩量——坑底收盘量 / 拉升峰值量 < 0.7 说明洗盘到位。
3. 拉升前是否窄幅横盘——单次清晰横盘为加分项。
4. 多次波段——窗口内多次「拉升-回踩」说明不是单次黄金坑，应降权。

正例：一鸣食品 7/27、百花医药 7/24、长源电力 8/10。
反例：河钢资源(已反弹+22%)、华邦健康(波段洗盘)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class GoldenPitParams:
    """黄金坑识别的可调参数。"""

    lookback_days: int = 60                     # 回看交易日数
    rally_min_gain_pct: float = 8.0             # 拉升累计涨幅下限（%）
    rally_max_gain_pct: float = 60.0            # 拉升累计涨幅上限（%）
    pit_min_drawdown_pct: float = 5.0           # 回撤下限（%）
    pit_max_drawdown_pct: float = 35.0          # 回撤上限（%）
    pit_volume_shrink: float = 0.7              # 坑底量 / 拉升峰值量 上限
    pit_max_days: int = 12                      # 拉升后到坑底最多交易日
    consolidation_min_days: int = 12            # 横盘最少交易日
    consolidation_max_range_pct: float = 16.0   # 横盘振幅上限（%）
    relaunch_max_bounce_pct: float = 12.0       # 坑底反弹超过此值=已起涨
    max_bounce_from_low_pct: float = 20.0       # 距60日低点累计涨幅上限（%），超过=已起涨
    # === 下跌中继护栏（捷成股份反例）===
    rally_start_drop_max_pct: float = 6.0       # 拉升起点当日跌幅上限（%），超过=暴跌日反抽，非底部健康拉升
    min_drawdown_from_high60_pct: float = 15.0  # 坑底距 60 日高点回撤下限（%），低于=刚从顶部下来，非低位坑底


@dataclass
class GoldenPitSignal:
    """黄金坑识别结果。"""

    matched: bool = False
    score: float = 0.0
    phase: str = "none"                       # none / pit_bottom / relaunch / extended
    rally_gain_pct: float = 0.0
    pit_drawdown_pct: float = 0.0
    pit_volume_ratio: float = 0.0             # 坑底量 / 拉升峰值量
    bounce_from_pit_pct: float = 0.0          # 距坑底反弹幅度
    bounce_from_low60_pct: float = 0.0        # 距60日低点累计涨幅
    consolidation_ok: bool = False
    multi_pit_count: int = 0
    # 技术面风险
    macd_dif: float = 0.0
    macd_dea: float = 0.0
    macd_below_zero: bool = False             # DIF 水下（< 0）
    macd_dead_cross: bool = False             # 死叉（DIF < DEA）
    drawdown_from_high60_pct: float = 0.0     # 距 60 日高点回撤（上方套牢盘近似）
    notes: list[str] = field(default_factory=list)


def detect_golden_pit(
    df: pd.DataFrame,
    params: Optional[GoldenPitParams] = None,
) -> GoldenPitSignal:
    """检测单只股票的黄金坑形态。df 需含 close（可选 high/low/volume/vol）。"""
    p = params or GoldenPitParams()
    sig = GoldenPitSignal()

    if df is None or len(df) < 40:
        sig.notes.append("日线不足 40 根")
        return sig

    df = df.reset_index(drop=True).copy()
    close = pd.to_numeric(df["close"], errors="coerce")
    high = pd.to_numeric(df["high"], errors="coerce") if "high" in df.columns else close
    low = pd.to_numeric(df["low"], errors="coerce") if "low" in df.columns else close
    vol_col = "volume" if "volume" in df.columns else ("vol" if "vol" in df.columns else None)
    volume = pd.to_numeric(df[vol_col], errors="coerce") if vol_col else None

    if close.isna().all():
        sig.notes.append("close 全为空")
        return sig

    n = len(df)
    lb = min(p.lookback_days, n)
    seg_start = n - lb

    # ---- 距 60 日低点累计涨幅（排除已起涨）----
    low60 = low.iloc[seg_start:].min()
    if low60 > 0:
        sig.bounce_from_low60_pct = (close.iloc[n - 1] - low60) / low60 * 100
    else:
        sig.bounce_from_low60_pct = 0.0
    if sig.bounce_from_low60_pct > p.max_bounce_from_low_pct:
        sig.notes.append(f"距60日低点已涨+{sig.bounce_from_low60_pct:.1f}%，已起涨")
        return sig

    # ---- 多次波段洗盘检测 ----
    sig.multi_pit_count = _count_pullbacks(close.iloc[seg_start:], p)

    # ---- 找最近一段「拉升 → 回踩坑底」结构 ----
    structure = _find_rally_pit(close, high, low, volume, n, p)
    if structure is None:
        sig.notes.append("未找到拉升-回踩结构")
        return sig

    (peak_idx, pit_idx, rally_gain, drawdown, pit_vol_ratio) = structure
    sig.rally_gain_pct = rally_gain
    sig.pit_drawdown_pct = drawdown
    sig.pit_volume_ratio = pit_vol_ratio

    pit_close = close.iloc[pit_idx]
    current_close = close.iloc[n - 1]
    bounce = (current_close - pit_close) / pit_close * 100
    sig.bounce_from_pit_pct = bounce

    # ---- 高位回落护栏：坑底距 60 日高点回撤过浅 → 刚从顶部下来，非低位坑底 ----
    # 捷成股份 8/4 见顶 5.72，8/14 坑底 5.10 仅回撤 10.8%，是冲高回落而非坑底。
    high60 = high.iloc[seg_start:].max()
    if high60 > 0:
        sig.drawdown_from_high60_pct = (pit_close - high60) / high60 * 100
        if sig.drawdown_from_high60_pct > -p.min_drawdown_from_high60_pct:
            sig.notes.append(
                f"坑底距60日高点仅回撤 {sig.drawdown_from_high60_pct:.1f}%，"
                f"刚从顶部回落，非低位坑底"
            )
            return sig

    # ---- 技术面风险：MACD 状态 ----
    if len(close) >= 26:
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()
        sig.macd_dif = float(dif.iloc[-1])
        sig.macd_dea = float(dea.iloc[-1])
        sig.macd_below_zero = sig.macd_dif < 0
        sig.macd_dead_cross = sig.macd_dif < sig.macd_dea

    # drawdown_from_high60_pct 已在"高位回落护栏"处以坑底口径计算，此处不再重复。

    # ---- Phase 1 横盘检测（拉升起点之前）----
    sig.consolidation_ok = _check_consolidation(close, high, low, peak_idx, p)

    # ---- 当前状态 ----
    days_since_pit = (n - 1) - pit_idx
    if days_since_pit <= 3 and bounce <= 2.0:
        sig.phase = "pit_bottom"
    elif bounce <= p.relaunch_max_bounce_pct:
        sig.phase = "relaunch"
    else:
        sig.phase = "extended"

    sig.score = _score(sig, p)
    sig.matched = sig.score >= 55.0
    return sig


def _find_rally_pit(close, high, low, volume, n, p):
    """找最近一段「拉升 → 回踩坑底」。

    返回 (peak_idx, pit_idx, rally_gain_pct, drawdown_pct, pit_vol_ratio)。
    找不到返回 None。
    """
    search_start = max(0, n - 20)

    # 1. 拉升高点：最近 20 根内最高收盘价（用 close 而非 high，避免长上影线假拉升）
    #    窗口过长会把更早的旧高点算进来（如 6 月的反弹高点），导致漏掉最近的拉升
    peak_idx = close.iloc[search_start:n - 1].idxmax()
    peak_price = close.iloc[peak_idx]

    # 2. 拉升起点：峰值往前 8 根内最低 close
    rally_start = peak_idx
    for i in range(peak_idx - 1, max(0, peak_idx - 8), -1):
        if close.iloc[i] < close.iloc[rally_start]:
            rally_start = i

    rally_gain = (peak_price - close.iloc[rally_start]) / close.iloc[rally_start] * 100
    if rally_gain < p.rally_min_gain_pct or rally_gain > p.rally_max_gain_pct:
        return None

    # 1b. 拉升起点当日暴跌检测（下跌中继护栏）：
    #     黄金坑的"拉升"起点应是底部横盘后的温和启动点，而不是暴跌日。
    #     捷成股份 7/24 暴跌 -8.2% 作为"起点"，实为暴跌后 V 型反抽，非健康拉升。
    if rally_start > 0 and close.iloc[rally_start - 1] > 0:
        rally_start_chg = (
            (close.iloc[rally_start] - close.iloc[rally_start - 1])
            / close.iloc[rally_start - 1]
            * 100
        )
        if rally_start_chg < -p.rally_start_drop_max_pct:
            return None

    # 3. 坑底：峰值之后 pit_max_days 天内最低 close
    pit_end = min(n, peak_idx + 1 + p.pit_max_days)
    if pit_end <= peak_idx + 1:
        return None
    pit_idx = close.iloc[peak_idx + 1:pit_end].idxmin()
    pit_close = close.iloc[pit_idx]
    drawdown = (peak_price - pit_close) / peak_price * 100
    if drawdown < p.pit_min_drawdown_pct or drawdown > p.pit_max_drawdown_pct:
        return None

    # 4. 坑底缩量：坑底量 / 拉升峰值量
    if volume is not None:
        rally_peak_vol = volume.iloc[rally_start:peak_idx + 1].max()
        pit_vol = volume.iloc[pit_idx]
        pit_vol_ratio = pit_vol / rally_peak_vol if rally_peak_vol > 0 else 1.0
        if pit_vol_ratio > p.pit_volume_shrink:
            return None
    else:
        pit_vol_ratio = 0.0

    return (peak_idx, pit_idx, rally_gain, drawdown, pit_vol_ratio)


def _check_consolidation(close, high, low, peak_idx, p):
    """检查拉升之前是否有一段窄幅横盘。"""
    start = peak_idx - p.consolidation_min_days
    if start < 0:
        return False
    seg_high = high.iloc[start:peak_idx].max()
    seg_low = low.iloc[start:peak_idx].min()
    if seg_low <= 0:
        return False
    rng = (seg_high - seg_low) / seg_low * 100
    return rng <= p.consolidation_max_range_pct


def _count_pullbacks(close, p):
    """统计窗口内明显回踩次数（近似波段洗盘次数）。"""
    count = 0
    peak = close.iloc[0]
    in_drawdown = False
    for i in range(1, len(close)):
        cur = close.iloc[i]
        if cur > peak:
            peak = cur
            in_drawdown = False
        else:
            dd = (peak - cur) / peak * 100
            if dd >= p.pit_min_drawdown_pct and not in_drawdown:
                count += 1
                in_drawdown = True
            elif dd < p.pit_min_drawdown_pct * 0.5:
                in_drawdown = False
    return count


def adjust_score_with_context(
    score: float,
    moneyflow: Optional[dict] = None,
    chip: Optional[dict] = None,
    board_heat_pct: Optional[float] = None,
) -> float:
    """叠加主力资金流 + 筹码分布 + 板块热度等正向指标，返回调整后的分数（0-100）。

    moneyflow 键：``mf_available`` / ``mf_net_inflow_5d``(万元) / ``mf_consecutive_days``。
    chip 键：``concentration_90``(越小越集中) / ``profit_ratio``(获利比例)。
    board_heat_pct：所属板块当日平均涨跌幅（%），主线板块加分。
    """
    # ---- 主力净流入（吸筹信号） ----
    if moneyflow and moneyflow.get("mf_available"):
        inflow_5d = moneyflow.get("mf_net_inflow_5d")
        if inflow_5d is not None:
            score += 8 if float(inflow_5d) > 0 else -8
        consec = moneyflow.get("mf_consecutive_days") or 0
        if consec >= 2:
            score += 3                     # 连续吸筹

    # ---- 筹码分布（单峰聚合 vs 多峰分散） ----
    if chip:
        conc90 = chip.get("concentration_90")
        if conc90 is not None:
            conc90 = float(conc90)
            if conc90 < 0.10:
                score += 10                # 单峰高度集中
            elif conc90 < 0.15:
                score += 6                 # 较集中
            elif conc90 >= 0.25:
                score -= 8                 # 分散 / 多峰（筹码乱）
        profit = chip.get("profit_ratio")
        if profit is not None:
            profit = float(profit)
            if profit < 0.30:
                score += 5                 # 上方套牢少
            elif profit > 0.70:
                score -= 5                 # 获利盘多，抛压

    # ---- 板块热度（主线/活跃板块加分） ----
    if board_heat_pct is not None:
        if board_heat_pct > 1.5:
            score += 6                     # 板块明显强势（主线）
        elif board_heat_pct > 0:
            score += 3                     # 板块温和走强
        elif board_heat_pct < -1.5:
            score -= 6                     # 板块明显走弱
        elif board_heat_pct < 0:
            score -= 3                     # 板块弱势

    return max(0.0, min(100.0, score))


def _score(sig: GoldenPitSignal, p: GoldenPitParams) -> float:
    """综合评分（0-100）。基础分 50，形态加分克制、技术面风险重扣，保证区分度。"""
    score = 50.0

    # ---- 形态加分（克制，避免轻易满分） ----
    if sig.phase == "pit_bottom":
        score += 10
    elif sig.phase == "relaunch":
        score += 8
    elif sig.phase == "extended":
        score -= 25

    # 反弹幅度（核心判据）
    if sig.bounce_from_pit_pct <= 8:
        score += 5
    elif sig.bounce_from_pit_pct <= p.relaunch_max_bounce_pct:
        score += 3
    else:
        score -= (sig.bounce_from_pit_pct - p.relaunch_max_bounce_pct) * 1.5

    # 横盘加分
    if sig.consolidation_ok:
        score += 5
    else:
        score -= 5

    # 回撤深度：8-25% 为理想黄金坑；太浅(<8%)只是震荡整理，太深(>30%)接近崩盘
    dd = sig.pit_drawdown_pct
    if dd < 5:
        score -= 10          # 基本没坑，只是震荡
    elif dd < 8:
        score -= 5           # 坑太浅
    elif dd <= 25:
        score += 5           # 理想回撤
    elif dd > 30:
        score -= 5           # 回撤过深

    # 缩量到位
    if 0 < sig.pit_volume_ratio <= 0.55:
        score += 5
    elif sig.pit_volume_ratio > 0.8:
        score -= 8

    # ---- 技术面风险 ----
    # MACD 死叉：动能转弱（东风死叉 → 扣分；一鸣/长源金叉 → 不扣）
    if sig.macd_dead_cross:
        score -= 15
    # MACD 金叉：动能向上，小幅加分（黄金坑底部水下金叉是正常反弹信号）
    if not sig.macd_dead_cross:
        score += 3

    # 多次波段洗盘惩罚（筹码分散）
    if sig.multi_pit_count > 1:
        score -= (sig.multi_pit_count - 1) * 12

    return max(0.0, min(100.0, score))
