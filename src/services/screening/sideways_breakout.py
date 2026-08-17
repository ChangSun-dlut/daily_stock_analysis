# -*- coding: utf-8 -*-
"""横盘蓄势突破识别器（Sideways Accumulation Breakout detector）。

专用形态识别，直接读取完整日线，识别
「长期窄幅横盘 → 均线高度粘合 → 缩量蓄势 → 放量突破」结构。

与黄金坑（golden_pit）的区别：
  - 黄金坑看"坑"：底部横盘 → 放量拉升 → 缩量回踩坑底 → 温和反弹企稳；
  - 横盘突破看"势"：长期窄幅横盘 + 均线粘合 + 缩量蓄势，等待放量突破。

核心判据（按重要性）：
1. **连续蓄势天数** —— 逐日判断是否满足横盘蓄势条件，统计连续天数。
   蓄势越久、突破越临近、可靠性越高（用户明确要求：连续多日选出的加权）。
2. 均线粘合度 —— MA5/10/20 间距越小，变盘越近。
3. 缩量程度 —— 蓄势期量能持续萎缩，洗盘越充分。
4. 突破信号 —— 当日放量 + 站上平台，或蓄势末期量能温和放大。

正例：澳洋健康(002172.SZ) 2026-08-03 ~ 08-11 连续 7 个交易日蓄势，
MA5/10/20 粘合 4.0~4.5%、量比 0.59~0.69，08-12 一字涨停突破。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class SidewaysBreakoutParams:
    """横盘蓄势突破识别的可调参数。"""

    lookback_days: int = 60                     # 回看交易日数
    ma_spread_max_pct: float = 6.0              # MA5/10/20 粘合度上限（%）
    amplitude_max_pct: float = 12.0             # 近 10 日收盘振幅上限（%）
    volume_shrink_max: float = 0.6              # 缩量率上限（近5日均量/近60日峰值均量，越小越缩量）
    bounce_from_low60_max_pct: float = 30.0     # 距 60 日低点累计涨幅上限（%），超过=已涨完
    consecutive_min_days: int = 3               # 最少连续蓄势天数（低于此不匹配）
    breakout_vol_ratio_min: float = 1.5         # 突破日量比（当日量 / 近 10 日均量）
    breakout_gain_min_pct: float = 2.0          # 突破日涨幅下限（%）
    breakout_gain_max_pct: float = 9.0          # 突破日涨幅上限（排除涨停一字板，不可参与）


@dataclass
class SidewaysBreakoutSignal:
    """横盘蓄势突破识别结果。"""

    matched: bool = False
    score: float = 0.0
    phase: str = "none"                       # none / accumulation / pre_breakout / breakout / extended
    consecutive_days: int = 0                 # 连续蓄势天数（核心加权项）
    ma_spread_pct: float = 0.0                # 当前 MA5/10/20 粘合度
    amplitude_pct: float = 0.0                # 近 10 日收盘振幅
    volume_ratio: float = 0.0                 # 近 10 日均量 / 前 10 日均量
    bounce_from_low60_pct: float = 0.0        # 距 60 日低点累计涨幅
    today_volume_ratio: float = 0.0           # 当日量 / 近 10 日均量
    today_gain_pct: float = 0.0               # 当日涨幅
    is_breakout_day: bool = False             # 当日是否放量突破
    macd_dif: float = 0.0
    macd_dea: float = 0.0
    macd_dead_cross: bool = False             # DIF < DEA
    notes: list[str] = field(default_factory=list)


def detect_sideways_breakout(
    df: pd.DataFrame,
    params: Optional[SidewaysBreakoutParams] = None,
) -> SidewaysBreakoutSignal:
    """检测单只股票的横盘蓄势突破形态。df 需含 close（可选 high/low/volume/vol）。"""
    p = params or SidewaysBreakoutParams()
    sig = SidewaysBreakoutSignal()

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
    ma5 = close.rolling(5).mean()
    ma10 = close.rolling(10).mean()
    ma20 = close.rolling(20).mean()

    # ---- 当前均线粘合度 ----
    m5, m10, m20 = ma5.iloc[n - 1], ma10.iloc[n - 1], ma20.iloc[n - 1]
    if pd.isna(m20):
        sig.notes.append("均线数据不足")
        return sig
    ma_min = min(m5, m10, m20)
    ma_max = max(m5, m10, m20)
    sig.ma_spread_pct = (ma_max - ma_min) / ma_min * 100 if ma_min > 0 else 999.0

    # ---- 近 10 日收盘振幅 ----
    seg_close = close.iloc[max(0, n - 10):n]
    if seg_close.min() > 0:
        sig.amplitude_pct = (seg_close.max() - seg_close.min()) / seg_close.min() * 100

    # ---- 缩量程度：近 5 日均量 / 近 60 日 5 日均量峰值（缩量率，越小越缩量） ----
    if volume is not None:
        v5 = volume.iloc[max(0, n - 5):n].mean()
        win = volume.iloc[max(0, n - 60):n]
        v_peak = win.rolling(5).mean().max() if len(win) >= 5 else v5
        sig.volume_ratio = (v5 / v_peak) if (v_peak and v_peak > 0) else 1.0
        # 当日量比（相对近 5 日均量）
        today_vol = volume.iloc[n - 1]
        sig.today_volume_ratio = (today_vol / v5) if (v5 and v5 > 0) else 1.0

    # ---- 当日涨幅 ----
    if n >= 2 and close.iloc[n - 2] > 0:
        sig.today_gain_pct = (close.iloc[n - 1] - close.iloc[n - 2]) / close.iloc[n - 2] * 100

    # ---- 距 60 日低点累计涨幅（排除已涨完） ----
    low60 = low.iloc[max(0, n - p.lookback_days):].min()
    if low60 > 0:
        sig.bounce_from_low60_pct = (close.iloc[n - 1] - low60) / low60 * 100
    if sig.bounce_from_low60_pct > p.bounce_from_low60_max_pct:
        sig.notes.append(f"距60日低点已涨+{sig.bounce_from_low60_pct:.1f}%，已涨完")
        return sig

    # ---- 连续蓄势天数（核心） ----
    sig.consecutive_days = _count_consecutive_accumulation(
        close, high, low, volume, ma5, ma10, ma20, n, p
    )
    if sig.consecutive_days < p.consecutive_min_days:
        sig.notes.append(f"连续蓄势天数不足（{sig.consecutive_days} < {p.consecutive_min_days}）")
        return sig

    # ---- 突破信号 ----
    sig.is_breakout_day = (
        sig.today_volume_ratio >= p.breakout_vol_ratio_min
        and p.breakout_gain_min_pct <= sig.today_gain_pct <= p.breakout_gain_max_pct
    )

    # ---- MACD ----
    if len(close) >= 26:
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()
        sig.macd_dif = float(dif.iloc[-1])
        sig.macd_dea = float(dea.iloc[-1])
        sig.macd_dead_cross = sig.macd_dif < sig.macd_dea

    # ---- 当前状态 ----
    if sig.is_breakout_day:
        sig.phase = "breakout"
    elif sig.today_volume_ratio >= 1.2:  # 量能温和放大，蓄势末期
        sig.phase = "pre_breakout"
    else:
        sig.phase = "accumulation"

    sig.score = _score(sig, p)
    sig.matched = sig.score >= 55.0
    return sig


def _is_accumulation_day(close, high, low, volume, ma5, ma10, ma20, i, p) -> bool:
    """判断第 i 个交易日是否满足"横盘蓄势"条件（用于统计连续天数）。"""
    m5, m10, m20 = ma5.iloc[i], ma10.iloc[i], ma20.iloc[i]
    if pd.isna(m20):
        return False
    ma_min = min(m5, m10, m20)
    ma_max = max(m5, m10, m20)
    if ma_min <= 0:
        return False
    # 1. 均线粘合
    spread = (ma_max - ma_min) / ma_min * 100
    if spread > p.ma_spread_max_pct:
        return False
    # 2. 近 10 日收盘振幅窄幅
    seg = close.iloc[max(0, i - 9):i + 1]
    if seg.min() <= 0:
        return False
    amp = (seg.max() - seg.min()) / seg.min() * 100
    if amp > p.amplitude_max_pct:
        return False
    # 3. 缩量蓄势：近 5 日均量 < 近 60 日 5 日均量峰值 × volume_shrink_max（量能萎缩到活跃期低位）
    if volume is not None:
        v5 = volume.iloc[max(0, i - 4):i + 1].mean()
        win = volume.iloc[max(0, i - 59):i + 1]
        if len(win) >= 5:
            v_peak = win.rolling(5).mean().max()
            if v_peak and v_peak > 0 and v5 > v_peak * p.volume_shrink_max:
                return False
    # 4. 收盘守稳 MA20（横盘中枢，允许在 MA5 附近小幅震荡，但不破位）
    if close.iloc[i] < m20:
        return False
    return True


def _count_consecutive_accumulation(close, high, low, volume, ma5, ma10, ma20, n, p) -> int:
    """从最近一个交易日起向前统计连续满足横盘蓄势条件的天数。"""
    count = 0
    for i in range(n - 1, -1, -1):
        if _is_accumulation_day(close, high, low, volume, ma5, ma10, ma20, i, p):
            count += 1
        else:
            break
    return count


def adjust_score_with_context(
    score: float,
    moneyflow: Optional[dict] = None,
    chip: Optional[dict] = None,
    board_heat_pct: Optional[float] = None,
) -> float:
    """叠加主力资金流 + 筹码分布 + 板块热度，返回调整后的分数（0-100）。

    与 golden_pit.adjust_score_with_context 语义一致，复用相同上下文信号。
    """
    # ---- 主力净流入（吸筹信号） ----
    if moneyflow and moneyflow.get("mf_available"):
        inflow_5d = moneyflow.get("mf_net_inflow_5d")
        if inflow_5d is not None:
            score += 8 if float(inflow_5d) > 0 else -8
        consec = moneyflow.get("mf_consecutive_days") or 0
        if consec >= 2:
            score += 3

    # ---- 筹码分布 ----
    if chip:
        conc90 = chip.get("concentration_90")
        if conc90 is not None:
            conc90 = float(conc90)
            if conc90 < 0.10:
                score += 10
            elif conc90 < 0.15:
                score += 6
            elif conc90 >= 0.25:
                score -= 8
        profit = chip.get("profit_ratio")
        if profit is not None:
            profit = float(profit)
            if profit < 0.30:
                score += 5
            elif profit > 0.70:
                score -= 5

    # ---- 板块热度 ----
    if board_heat_pct is not None:
        if board_heat_pct > 1.5:
            score += 6
        elif board_heat_pct > 0:
            score += 3
        elif board_heat_pct < -1.5:
            score -= 6
        elif board_heat_pct < 0:
            score -= 3

    return max(0.0, min(100.0, score))


def _score(sig: SidewaysBreakoutSignal, p: SidewaysBreakoutParams) -> float:
    """综合评分（0-100）。基础分 45，连续蓄势天数为核心加权项，保证区分度。"""
    score = 45.0

    # ---- 连续蓄势天数加权（核心：连续多日选出的增加权重） ----
    cd = sig.consecutive_days
    if cd >= 15:
        score += 22
    elif cd >= 10:
        score += 18
    elif cd >= 7:
        score += 14
    elif cd >= 5:
        score += 9
    elif cd >= 3:
        score += 5

    # ---- 均线粘合度（越紧变盘越近） ----
    if sig.ma_spread_pct < 3.0:
        score += 8
    elif sig.ma_spread_pct < 4.5:
        score += 5
    elif sig.ma_spread_pct < p.ma_spread_max_pct:
        score += 2

    # ---- 缩量程度（越缩洗盘越充分；volume_ratio 为近5日均量/峰值均量） ----
    if sig.volume_ratio < 0.4:
        score += 8
    elif sig.volume_ratio < 0.6:
        score += 5
    elif sig.volume_ratio < 0.8:
        score += 2

    # ---- 突破信号 ----
    if sig.is_breakout_day:
        score += 8
    elif sig.phase == "pre_breakout":
        score += 3

    # ---- 距 60 日低点涨幅惩罚（已涨太多则降权） ----
    if sig.bounce_from_low60_pct > 20.0:
        score -= (sig.bounce_from_low60_pct - 20.0) * 0.5

    # ---- MACD ----
    if sig.macd_dead_cross:
        score -= 12
    else:
        score += 3

    return max(0.0, min(100.0, score))
