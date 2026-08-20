import type React from 'react';
import { Loader2 } from 'lucide-react';
import { useUiLanguage } from '../../contexts/UiLanguageContext';

const VOLUME_RATIO_SPIKE_THRESHOLD = 2;
const VOLUME_RATIO_SURGE_THRESHOLD = 5;

export type VolumeRatioLevel = 'surge' | 'spike' | 'normal' | 'unavailable';

export function classifyVolumeRatio(ratio: number | null | undefined): VolumeRatioLevel {
  if (ratio === null || ratio === undefined || !Number.isFinite(ratio)) return 'unavailable';
  if (ratio >= VOLUME_RATIO_SURGE_THRESHOLD) return 'surge';
  if (ratio >= VOLUME_RATIO_SPIKE_THRESHOLD) return 'spike';
  return 'normal';
}

/**
 * 放量预警徽标：在自选股列表和历史列表中复用。
 * - surge: 量比 ≥ 5，红色脉冲
 * - spike: 量比 ≥ 2，黄色
 * - normal: 量比 < 2，灰色
 * - unavailable: 数据缺失，cyan 占位
 */
export const VolumeSpikeBadge: React.FC<{
  ratio: number | null | undefined;
  changePercent: number | null | undefined;
  loading?: boolean;
  /** 自定义 data-testid 前缀，避免两个栏目重名（例如 watchlist-volume-spike / history-volume-spike）。默认 watchlist-volume-* */
  testIdPrefix?: string;
}> = ({ ratio, changePercent, loading, testIdPrefix = 'watchlist-volume' }) => {
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
    return (
      <span
        className="inline-flex h-6 items-center gap-1 rounded-full border border-cyan/40 bg-cyan/15 px-2 text-[11px] font-medium leading-none text-cyan"
        title={t('watchlist.volumeRatioTooltip')}
        data-testid={`${testIdPrefix}-unavailable`}
      >
        <span className="h-1.5 w-1.5 rounded-full bg-cyan" aria-hidden="true" />
        {t('watchlist.volumeRatioUnavailable')}
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
