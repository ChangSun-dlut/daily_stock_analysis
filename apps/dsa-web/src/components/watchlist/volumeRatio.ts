// 量比分级工具：被 VolumeSpikeBadge 复用，单独抽离以满足
// react-refresh/only-export-components（组件文件只能导出组件）。
export const VOLUME_RATIO_SPIKE_THRESHOLD = 2;
export const VOLUME_RATIO_SURGE_THRESHOLD = 5;

export type VolumeRatioLevel = 'surge' | 'spike' | 'normal' | 'unavailable';

export function classifyVolumeRatio(ratio: number | null | undefined): VolumeRatioLevel {
  if (ratio === null || ratio === undefined || !Number.isFinite(ratio)) return 'unavailable';
  if (ratio >= VOLUME_RATIO_SURGE_THRESHOLD) return 'surge';
  if (ratio >= VOLUME_RATIO_SPIKE_THRESHOLD) return 'spike';
  return 'normal';
}
