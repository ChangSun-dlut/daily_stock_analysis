import apiClient from './index';
import { systemConfigApi } from './systemConfig';
import { toCamelCase } from './utils';

const ALPHASIFT_SCREEN_TIMEOUT_MS = 180000;
const ALPHASIFT_INSTALL_TIMEOUT_MS = 300000;
export const ALPHASIFT_CONFIG_CHANGED_EVENT = 'alphasift-config-changed';
export const SYSTEM_CONFIG_CHANGED_EVENT = 'dsa-system-config-changed';

export type AlphaSiftStatus = {
  enabled: boolean;
  available: boolean;
  installSpecIsDefault: boolean;
  contractVersion?: string | null;
  version?: string | null;
  strategyCount?: number | null;
  sourceHealth?: Record<string, Record<string, Record<string, unknown>>>;
  diagnostics?: Record<string, string>;
};

export type AlphaSiftInstallResponse = {
  installed: boolean;
  alreadyInstalled: boolean;
  installSpecIsDefault: boolean;
};

export type AlphaSiftCandidate = {
  rank: number;
  code: string;
  name: string;
  score?: number | null;
  screenScore?: number | null;
  reason: string;
  riskLevel?: string;
  riskFlags?: string[];
  llmScore?: number | null;
  llmConfidence?: number | null;
  mfNetInflow?: number | null;
  mfNetInflow5D?: number | null;
  mfNetInflow5d?: number | null;
  mfConsecutiveDays?: number | null;
  mfInflowStrengthPct?: number | null;
  mfAvailable?: boolean | null;
  llmSector?: string;
  llmTheme?: string;
  llmTags?: string[];
  llmThesis?: string;
  llmCatalysts?: string[];
  llmRisks?: string[];
  llmWatchItems?: string[];
  llmInvalidators?: string[];
  llmStyleFit?: string;
  price?: number | null;
  changePct?: number | null;
  amount?: number | null;
  industry?: string;
  factorScores?: Record<string, number>;
  postAnalysisSummaries?: Record<string, string>;
  postAnalysisTags?: string[];
  dsaContext?: {
    enriched?: boolean;
    quote?: Record<string, unknown>;
    fundamentals?: Record<string, unknown>;
    news?: {
      success?: boolean;
      query?: string;
      provider?: string;
      results?: Array<Record<string, unknown>>;
      error?: string | null;
    };
    warnings?: string[];
  };
  dsaNews?: Array<{
    title?: string;
    snippet?: string;
    url?: string;
    source?: string;
    publishedDate?: string | null;
  }>;
  dsaAnalysisSummary?: string;
  raw: Record<string, unknown>;
};

export type AlphaSiftStrategy = {
  id: string;
  name: string;
  title?: string;
  description: string;
  version?: string;
  category?: string;
  tag?: string;
  tags?: string[];
  marketScope?: string[];
  market?: string;
};

export type AlphaSiftStrategiesResponse = {
  enabled: boolean;
  strategies: AlphaSiftStrategy[];
  strategyCount: number;
};

export type AlphaSiftHotspot = {
  topic: string;
  name?: string;
  source?: string;
  rank?: number | null;
  changePct?: number | null;
  heatScore?: number | null;
  trendScore?: number | null;
  persistenceScore?: number | null;
  coolingScore?: number | null;
  observations?: number | null;
  state?: string;
  stage?: string;
  sampleStockCount?: number | null;
  leaders?: string[];
  providerUsed?: string;
  fallbackUsed?: boolean;
  cacheUsed?: boolean;
  cachedAt?: string | null;
  sourceErrors?: string[];
  stale?: boolean;
  staleAgeHours?: number | null;
};

export type AlphaSiftHotspotRouteItem = {
  title: string;
  description: string;
  source?: string;
  date?: string;
  time?: string;
  publishedAt?: string;
  url?: string;
};

export type AlphaSiftHotspotStock = {
  code?: string;
  name?: string;
  changePct?: number | null;
  amount?: number | null;
  turnoverRate?: number | null;
  volumeRatio?: number | null;
  role?: string;
  hotStockScore?: number | null;
  source?: string;
  sourceConfidence?: number | null;
  fallbackUsed?: boolean;
};

export type AlphaSiftHotspotDetail = {
  enabled: boolean;
  provider: string;
  topic: string;
  name?: string;
  canonicalTopic?: string;
  aliases?: string[];
  summary?: string;
  summaryDetail?: Record<string, unknown>;
  route: AlphaSiftHotspotRouteItem[];
  timeline?: AlphaSiftHotspotRouteItem[];
  stocks: AlphaSiftHotspotStock[];
  leaderStocks?: AlphaSiftHotspotStock[];
  stockCount: number;
  sourceErrors?: string[];
  qualityStatus?: 'available' | 'partial' | 'stale' | 'failed' | string;
  missingFields?: string[];
  fallbackUsed?: boolean;
  stale?: boolean;
  staleAgeHours?: number | null;
  cacheUsed?: boolean;
  cachedAt?: string | null;
  resolverCandidates?: Record<string, unknown>[];
};

export type AlphaSiftHotspotsResponse = {
  enabled: boolean;
  provider: string;
  providerUsed?: string;
  fallbackUsed?: boolean;
  cacheUsed?: boolean;
  cachedAt?: string | null;
  sourceErrors?: string[];
  stale?: boolean;
  staleAgeHours?: number | null;
  message?: string | null;
  hotspots: AlphaSiftHotspot[];
  hotspotCount: number;
  details?: Record<string, AlphaSiftHotspotDetail>;
};

export type AlphaSiftScreenResponse = {
  enabled: boolean;
  candidates: AlphaSiftCandidate[];
  candidateCount: number;
  runId?: string;
  strategy?: string;
  market?: string;
  snapshotCount?: number;
  afterFilterCount?: number;
  llmRanked?: boolean;
  llmMarketView?: string;
  llmSelectionLogic?: string;
  llmPortfolioRisk?: string;
  llmCoverage?: number | null;
  llmParseErrors?: string[];
  warnings?: string[];
  sourceErrors?: string[];
  dsaEnrichment?: {
    enabled?: boolean;
    maxCandidates?: number;
    requestedCount?: number;
    enrichedCount?: number;
    warnings?: string[];
  };
  deepAnalysisRequested?: boolean | null;
  postAnalyzers?: string[];
  dailyEnriched?: boolean | null;
  dailyEnrichCount?: number | null;
  riskEnabled?: boolean | null;
  portfolioDiversityEnabled?: boolean | null;
  portfolioConcentrationNotes?: string[];
};

export type AlphaSiftScreenAccepted = {
  taskId: string;
  traceId?: string | null;
  status: 'pending' | 'processing' | 'completed' | 'failed' | string;
  message: string;
  strategy: string;
  market: string;
  maxResults: number;
};

export type AlphaSiftScreenTaskStatus = {
  taskId: string;
  traceId?: string | null;
  status: 'pending' | 'processing' | 'completed' | 'failed' | string;
  progress?: number | null;
  message?: string | null;
  error?: string | null;
  result?: AlphaSiftScreenResponse | null;
};

// ——— 一键回测 ———
export type BacktestPattern = {
  range20dPct: number;
  volatility20dPct: number;
  change20dPct: number;
  consolidationDays: number;
  volumeRatio: number;
  signalDayReturnPct: number;
  allPass: boolean;
  violations: string[];
};

export type BacktestHoldingReturn = {
  days: number;
  returnPct: number;
};

export type BacktestMaSnapshot = {
  ma: number;
  value: number;
  priceAbove: boolean;
};

export type BacktestWindowSimulation = {
  signalDate: string;
  buyPrice: number;
  holdDays: number;
  holdReturnPct: number;
  patternAllPass: boolean;
  range20d: number;
  volatility20d: number;
  change20d: number;
  consolidationDays: number;
};

export type BacktestCandidate = {
  code: string;
  name: string;
  signalDate: string;
  signalPrice: number;
  pattern: BacktestPattern | null;
  holdingReturns: BacktestHoldingReturn[];
  maxReturn5dPct: number | null;
  minReturn5dPct: number | null;
  maSnapshots: BacktestMaSnapshot[];
  windowSimulations: BacktestWindowSimulation[];
  error: string | null;
};

export type BacktestResponse = {
  strategy: string;
  signalDate: string;
  candidates: BacktestCandidate[];
  summary: {
    totalCandidates: number;
    validCount: number;
    errorCount: number;
    profitableCount: number;
    patternPassCount: number;
    allProfitable: boolean;
    allPatternOk: boolean;
  };
};

// ---------- 昨日复盘 ----------
export type YesterdayBacktestPattern = {
  range20dPct: number;
  volatility20dPct: number;
  change20dPct: number;
  consolidationDays: number;
  volumeRatio: number;
  signalDayReturnPct: number;
  allPass: boolean;
  violations: string[];
};

export type TrendSnapshot = {
  trendStatus: string;
  trendStrength: number;
  maAlignment: string;
  ma5: number;
  ma10: number;
  ma20: number;
  ma60: number;
  biasMa5: number;
  volumeStatus: string;
  volumeRatio5d: number;
  macdDif: number;
  macdDea: number;
  macdBar: number;
  macdStatus: string;
  macdSignal: string;
  rsi6: number;
  rsi12: number;
  rsi24: number;
  rsiStatus: string;
  buySignal: string;
  signalScore: number;
};

export type YesterdayBacktestCandidate = {
  code: string;
  name: string;
  signalDate: string;
  todayDate: string;
  signalPrice: number;
  todayOpen: number;
  todayClose: number;
  todayHigh: number;
  todayLow: number;
  todayReturnPct: number;
  todayHighReturnPct: number;
  todayLowReturnPct: number;
  pattern: YesterdayBacktestPattern | null;
  volumeRatio: number;
  hasAnomaly: boolean;
  anomalyReasons: string[];
  error: string | null;
  trend: TrendSnapshot | null;
};

export type YesterdayBacktestResponse = {
  candidates: YesterdayBacktestCandidate[];
  total: number;
  anomalyCount: number;
  avgReturnPct: number;
  signalDate: string;
  todayDate: string;
  errors: string[];
};

export function notifyAlphaSiftConfigChanged(): void {
  window.dispatchEvent(new Event(ALPHASIFT_CONFIG_CHANGED_EVENT));
  notifySystemConfigChanged();
}

export function notifySystemConfigChanged(): void {
  window.dispatchEvent(new Event(SYSTEM_CONFIG_CHANGED_EVENT));
}

async function setAlphaSiftEnabled(value: 'true' | 'false'): Promise<void> {
  const config = await systemConfigApi.getConfig(false);
  await systemConfigApi.update({
    configVersion: config.configVersion,
    maskToken: config.maskToken,
    reloadNow: true,
    items: [{ key: 'ALPHASIFT_ENABLED', value }],
  });
  notifyAlphaSiftConfigChanged();
}

export const alphasiftApi = {
  async getStatus(): Promise<AlphaSiftStatus> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/screening/status');
    return toCamelCase<AlphaSiftStatus>(response.data);
  },

  async screen(payload: { market: string; strategy: string; maxResults: number }): Promise<AlphaSiftScreenResponse> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/screening/screen', {
      market: payload.market,
      strategy: payload.strategy,
      max_results: payload.maxResults,
    }, { timeout: ALPHASIFT_SCREEN_TIMEOUT_MS });
    return toCamelCase<AlphaSiftScreenResponse>(response.data);
  },

  async startScreen(payload: { market: string; strategy: string; maxResults: number }): Promise<AlphaSiftScreenAccepted> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/screening/screen/tasks', {
      market: payload.market,
      strategy: payload.strategy,
      max_results: payload.maxResults,
    });
    return toCamelCase<AlphaSiftScreenAccepted>(response.data);
  },

  async getScreenTask(taskId: string): Promise<AlphaSiftScreenTaskStatus> {
    const response = await apiClient.get<Record<string, unknown>>(`/api/v1/screening/screen/tasks/${encodeURIComponent(taskId)}`);
    return toCamelCase<AlphaSiftScreenTaskStatus>(response.data);
  },

  async getScreenCache(strategy: string): Promise<Record<string, unknown> | null> {
    const response = await apiClient.get<Record<string, unknown> | null>(`/api/v1/screening/screen/cache/${encodeURIComponent(strategy)}`);
    if (!response.data) return null;
    return toCamelCase<Record<string, unknown>>(response.data);
  },

  async getStrategies(): Promise<AlphaSiftStrategiesResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/screening/strategies', { timeout: ALPHASIFT_INSTALL_TIMEOUT_MS });
    return toCamelCase<AlphaSiftStrategiesResponse>(response.data);
  },

  async getHotspots(payload: { provider?: string; top?: number; refresh?: boolean; includeDetails?: boolean } = {}): Promise<AlphaSiftHotspotsResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/screening/hotspots', {
      params: {
        provider: payload.provider || 'akshare',
        top: payload.top ?? 12,
        refresh: payload.refresh ?? false,
        include_details: payload.includeDetails ?? true,
      },
      timeout: ALPHASIFT_INSTALL_TIMEOUT_MS,
    });
    const normalized = toCamelCase<AlphaSiftHotspotsResponse>(response.data);
    if (normalized.details) {
      const detailsByTopic: Record<string, AlphaSiftHotspotDetail> = {};
      Object.values(normalized.details).forEach((detail) => {
        if (detail?.topic) {
          detailsByTopic[detail.topic] = detail;
        }
      });
      normalized.details = { ...normalized.details, ...detailsByTopic };
    }
    return normalized;
  },

  async getHotspotDetail(payload: { topic: string; provider?: string; refresh?: boolean }): Promise<AlphaSiftHotspotDetail> {
    const response = await apiClient.get<Record<string, unknown>>(
      `/api/v1/screening/hotspots/${encodeURIComponent(payload.topic)}`,
      {
        params: { provider: payload.provider || 'akshare', refresh: payload.refresh ?? false },
        timeout: ALPHASIFT_INSTALL_TIMEOUT_MS,
      },
    );
    return toCamelCase<AlphaSiftHotspotDetail>(response.data);
  },

  async install(): Promise<AlphaSiftInstallResponse> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/screening/install', {}, { timeout: ALPHASIFT_INSTALL_TIMEOUT_MS });
    return toCamelCase<AlphaSiftInstallResponse>(response.data);
  },

  async enable(): Promise<void> {
    await setAlphaSiftEnabled('true');
    try {
      const status = await alphasiftApi.getStatus();
      if (!status.available) {
        const reason = status.diagnostics?.reason ? `（${status.diagnostics.reason}）` : '';
        throw new Error(`AlphaSift 适配层不可用${reason}。请确认后端已安装项目依赖，必要时执行 pip install -r requirements.txt 或重建 Docker/桌面后端。`);
      }
    } catch (error) {
      try {
        await setAlphaSiftEnabled('false');
      } catch {
        // Preserve the original install/status failure for the caller.
      }
      throw error;
    }
  },

  async backtest(payload: {
    strategy: string;
    candidates: Array<{ code: string; name: string; price?: number | null }>;
  }): Promise<BacktestResponse> {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/screening/screen/backtest',
      payload,
      { timeout: 120000 },
    );
    return toCamelCase<BacktestResponse>(response.data);
  },

  async backtestYesterday(payload: {
    strategy: string;
    candidates: Array<{ code: string; name: string; price?: number | null }>;
  }): Promise<YesterdayBacktestResponse> {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/screening/screen/backtest/yesterday',
      payload,
      { timeout: 120000 },
    );
    return toCamelCase<YesterdayBacktestResponse>(response.data);
  },
};
