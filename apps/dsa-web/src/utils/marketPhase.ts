import type {
  AnalysisPhase,
  MarketPhaseSummary,
  MarketPhaseValue,
  ReportLanguage,
} from '../types/analysis';
import { normalizeReportLanguage } from './reportLanguage';

const REQUEST_PHASE_LABELS: Record<ReportLanguage, Record<AnalysisPhase, string>> = {
  zh: {
    auto: '自动阶段',
    premarket: '盘前',
    intraday: '盘中',
    postmarket: '盘后',
  },
  en: {
    auto: 'Auto',
    premarket: 'Pre-market',
    intraday: 'Intraday',
    postmarket: 'Post-market',
  },
  ko: {
    auto: '자동 단계',
    premarket: '장 시작 전',
    intraday: '장중',
    postmarket: '장 마감 후',
  },
};

const MARKET_PHASE_LABELS: Record<ReportLanguage, Record<MarketPhaseValue, string>> = {
  zh: {
    premarket: '盘前',
    intraday: '盘中',
    lunch_break: '午间休市',
    closing_auction: '临近收盘',
    postmarket: '盘后',
    non_trading: '非交易日',
    unknown: '阶段未知',
  },
  en: {
    premarket: 'Pre-market',
    intraday: 'Intraday',
    lunch_break: 'Lunch break',
    closing_auction: 'Near close',
    postmarket: 'Post-market',
    non_trading: 'Non-trading',
    unknown: 'Unknown phase',
  },
  ko: {
    premarket: '장 시작 전',
    intraday: '장중',
    lunch_break: '점심 휴장',
    closing_auction: '마감 임박',
    postmarket: '장 마감 후',
    non_trading: '비거래일',
    unknown: '단계 불명',
  },
};

const TEXT = {
  zh: {
    requestPrefix: '请求阶段',
    finalPrefix: '市场阶段',
    partialBar: '日线未完成',
  },
  en: {
    requestPrefix: 'Requested phase',
    finalPrefix: 'Market phase',
    partialBar: 'Partial bar',
  },
  ko: {
    requestPrefix: '요청 단계',
    finalPrefix: '시장 단계',
    partialBar: '일봉 미완성',
  },
} as const;

export const getRequestedPhaseLabel = (
  phase?: AnalysisPhase | null,
  language?: ReportLanguage | null,
): string | null => {
  if (!phase) {
    return null;
  }

  const reportLanguage = normalizeReportLanguage(language);
  const label = REQUEST_PHASE_LABELS[reportLanguage][phase];
  if (!label) {
    return null;
  }

  return `${TEXT[reportLanguage].requestPrefix}: ${label}`;
};

export const getMarketPhaseSummaryLabel = (
  summary?: MarketPhaseSummary | null,
  language?: ReportLanguage | null,
): string | null => {
  if (!summary) {
    return null;
  }

  const reportLanguage = normalizeReportLanguage(language);
  const phaseLabel = MARKET_PHASE_LABELS[reportLanguage][summary.phase];
  if (!phaseLabel) {
    return null;
  }

  const market = (summary.market || '').trim().toUpperCase();
  const value = market ? `${market} · ${phaseLabel}` : phaseLabel;
  return `${TEXT[reportLanguage].finalPrefix}: ${value}`;
};

export const getPartialBarLabel = (language?: ReportLanguage | null): string =>
  TEXT[normalizeReportLanguage(language)].partialBar;

/**
 * 根据当前 UTC 时间推算 A 股（上海/深圳）所处交易阶段。
 *
 * 交易时段（北京时间）：
 * - 09:30 ~ 11:30  盘中
 * - 11:30 ~ 13:00  午间休市
 * - 13:00 ~ 15:00  盘中
 * - 其他时段 / 周末  盘前/盘后/非交易日
 *
 * 节假日不单独处理，因为后端量比自算也是按当前时刻是否落在交易时段判断；
 * UI 展示只需与后端逻辑保持一致即可。
 */
export const getCurrentAshareMarketPhase = (
  now: Date = new Date(),
): MarketPhaseValue => {
  const shanghaiTime = now.toLocaleString('sv-SE', {
    timeZone: 'Asia/Shanghai',
    hour12: false,
  });
  const [, timePart] = shanghaiTime.split(' ');
  const [hour, minute] = (timePart || '00:00').split(':').map(Number);
  const minutes = hour * 60 + minute;
  const weekday = now.toLocaleDateString('en-US', {
    timeZone: 'Asia/Shanghai',
    weekday: 'short',
  });
  const isWeekday = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'].includes(weekday);

  if (!isWeekday) {
    return 'non_trading';
  }
  if (minutes < 9 * 60 + 30) {
    return 'premarket';
  }
  if (minutes < 11 * 60 + 30) {
    return 'intraday';
  }
  if (minutes < 13 * 60) {
    return 'lunch_break';
  }
  if (minutes < 15 * 60) {
    return 'intraday';
  }
  return 'postmarket';
};
