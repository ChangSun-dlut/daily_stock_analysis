import type React from 'react';
import { Loader2 } from 'lucide-react';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import { getCurrentAshareMarketPhase } from '../../utils/marketPhase';
import type { MarketPhaseValue } from '../../types/analysis';
import { classifyVolumeRatio } from './volumeRatio';

import type { UiTextKey } from '../../i18n/uiText';

const PHASE_TO_VOLUME_RATIO_KEY: Record<MarketPhaseValue, UiTextKey> = {
  premarket: 'watchlist.volumeRatioPremarket',
  lunch_break: 'watchlist.volumeRatioLunchBreak',
  postmarket: 'watchlist.volumeRatioPostmarket',
  non_trading: 'watchlist.volumeRatioNonTrading',
  intraday: 'watchlist.volumeRatioUnavailable',
  closing_auction: 'watchlist.volumeRatioUnavailable',
  unknown: 'watchlist.volumeRatioUnavailable',
};

/**
 * 放量预警徽标：在自选股列表和历史列表中复用。
 * - surge: 量比 ≥ 5，红色脉冲
 * - spike: 量比 ≥ 2，黄色
 * - normal: 量比 < 2，灰色
 * - unavailable: 数据缺失或 A 股非交易时段，cyan 占位；会根据当前北京时间显示
 *   「未开盘 / 午休 / 已收盘 / 非交易日 / 量比暂无」。
 */
export const VolumeSpikeBadge: React.FC<{
  ratio: number | null | undefined;
  changePercent: number | null | undefined;
  loading?: boolean;
  /** 股票所属市场；A 股（cn）在量比缺失时可根据交易阶段给出更精确提示。 */
  market?: string | null;
  /** 自定义 data-testid 前缀，避免两个栏目重名（例如 watchlist-volume-spike / history-volume-spike）。默认 watchlist-volume-* */
  testIdPrefix?: string;
}> = ({ ratio, changePercent, loading, market, testIdPrefix = 'watchlist-volume' }) => {
  const { t } = useUiLanguage();
  if (loading) {
    return (
      <span
        className="inline-flex h-6 items-center gap-1 rounded-full border border-subtle-hover bg-base/40 px-2 text-[11px] font-medium leading-none text-muted-text"
        aria-label={t('watchlist.todayStatusLoading')}
      >
        <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
      </span>
    );
  }
  const level = classifyVolumeRatio(ratio);
  if (level === 'unavailable') {
    const phase = (market || '').trim().toLowerCase() === 'cn'
      ? getCurrentAshareMarketPhase()
      : 'unknown';
    const labelKey = PHASE_TO_VOLUME_RATIO_KEY[phase] || 'watchlist.volumeRatioUnavailable';
    return (
      <span
        className="inline-flex h-6 items-center gap-1 rounded-full border border-cyan/40 bg-cyan/15 px-2 text-[11px] font-medium leading-none text-cyan"
        title={t('watchlist.volumeRatioTooltip')}
        data-testid={`${testIdPrefix}-unavailable`}
      >
        <span className="h-1.5 w-1.5 rounded-full bg-cyan" aria-hidden="true" />
        {t(labelKey)}
      </span>
    );
  }
  if (level === 'normal') {
    return (
      <span
        className="inline-flex h-6 items-center gap-1 rounded-full border border-subtle-hover bg-base/60 px-2 text-[11px] font-medium leading-none text-secondary-text"
        title={t('watchlist.volumeRatioTooltip')}
        data-testid={`${testIdPrefix}-normal`}
      >
        <span className="h-1.5 w-1.5 rounded-full bg-secondary-text" aria-hidden="true" />
        {t('watchlist.volumeNormal')}
      </span>
    );
  }
  const ratioText = typeof ratio === 'number' ? ratio.toFixed(2) : '—';
  const labelKey = level === 'surge' ? 'watchlist.volumeSurge' : 'watchlist.volumeSpike';
  const tone =
    level === 'surge'
      ? 'border-danger/40 bg-danger/15 text-danger animate-pulse shadow-[0_0_8px_rgba(239,68,68,0.4)]'
      : 'border-warning/40 bg-warning/15 text-warning';
  const changeText =
    typeof changePercent === 'number' && Number.isFinite(changePercent)
      ? `${changePercent >= 0 ? '+' : ''}${changePercent.toFixed(2)}%`
      : null;
  return (
    <span
      className={`inline-flex h-6 items-center gap-1 rounded-full border px-2 text-[11px] font-semibold leading-none ${tone}`}
      title={t('watchlist.volumeRatioTooltip')}
      data-testid={`${testIdPrefix}-${level}`}
    >
      <span>{t(labelKey, { ratio: ratioText })}</span>
      {changeText ? <span className="opacity-80">{changeText}</span> : null}
    </span>
  );
};
