import apiClient from './index';
import { createParsedApiError, getParsedApiError, type ParsedApiError } from './error';
import { toCamelCase } from './utils';
import { normalizeStockCode } from '../utils/stockCode';

/** /stocks/watchlist/spot-quotes 返回的单股行情载荷（仅取前端需要的字段） */
export interface WatchlistQuotePayload {
  stockCode?: string;
  stockName?: string;
  currentPrice?: number | null;
  changePercent?: number | null;
  amount?: number | null;
  volumeRatio?: number | null;
  updateTime?: string | null;
}

/** 前端 watchlist 行使用的视图（去重失败字段） */
export interface WatchlistSpotQuoteView {
  volumeRatio: number | null;
  changePercent: number | null;
  amount: number | null;
  error: string | null;
}
import type {
  AgentBackendStatusPreviewRequest,
  AgentBackendStatusResponse,
  DiscoverLLMChannelModelsRequest,
  DiscoverLLMChannelModelsResponse,
  ExportSystemConfigResponse,
  GenerationBackendStatusPreviewRequest,
  GenerationBackendStatusResponse,
  ImportSystemConfigRequest,
  SchedulerRunNowResponse,
  SchedulerStatusResponse,
  SetupStatusResponse,
  SystemConfigConflictResponse,
  SystemConfigResponse,
  SystemConfigSchemaResponse,
  SystemConfigValidationErrorResponse,
  TestLLMChannelRequest,
  TestLLMChannelResponse,
  TestGenerationBackendRequest,
  TestGenerationBackendResponse,
  TestNotificationChannelRequest,
  TestNotificationChannelResponse,
  UpdateSystemConfigRequest,
  UpdateSystemConfigResponse,
  ValidateSystemConfigRequest,
  ValidateSystemConfigResponse,
} from '../types/systemConfig';

export class SystemConfigValidationError extends Error {
  issues: SystemConfigValidationErrorResponse['issues'];
  parsedError: ParsedApiError;

  constructor(message: string, issues: SystemConfigValidationErrorResponse['issues'], parsedError?: ParsedApiError) {
    super(message);
    this.name = 'SystemConfigValidationError';
    this.issues = issues;
    this.parsedError = parsedError ?? createParsedApiError({
      title: '配置校验失败',
      message,
      rawMessage: message,
      status: 400,
      category: 'http_error',
    });
  }
}

export class SystemConfigConflictError extends Error {
  currentConfigVersion?: string;
  parsedError: ParsedApiError;

  constructor(message: string, currentConfigVersion?: string, parsedError?: ParsedApiError) {
    super(message);
    this.name = 'SystemConfigConflictError';
    this.currentConfigVersion = currentConfigVersion;
    this.parsedError = parsedError ?? createParsedApiError({
      title: '配置版本冲突',
      message,
      rawMessage: message,
      status: 409,
      category: 'http_error',
    });
  }
}

function toSnakeUpdatePayload(payload: UpdateSystemConfigRequest): Record<string, unknown> {
  return {
    config_version: payload.configVersion,
    mask_token: payload.maskToken ?? '******',
    reload_now: payload.reloadNow ?? true,
    items: payload.items.map((item) => ({
      key: item.key,
      value: item.value,
    })),
  };
}

function toSnakeValidatePayload(payload: ValidateSystemConfigRequest): Record<string, unknown> {
  return {
    items: payload.items.map((item) => ({
      key: item.key,
      value: item.value,
    })),
  };
}

function toSnakeImportPayload(payload: ImportSystemConfigRequest): Record<string, unknown> {
  return {
    config_version: payload.configVersion,
    content: payload.content,
    reload_now: payload.reloadNow ?? true,
  };
}

function toSnakeTestChannelPayload(payload: TestLLMChannelRequest): Record<string, unknown> {
  const request: Record<string, unknown> = {
    name: payload.name,
    protocol: payload.protocol,
    api_surface: payload.apiSurface ?? 'chat_completions',
    base_url: payload.baseUrl ?? '',
    api_key: payload.apiKey ?? '',
    models: payload.models,
    enabled: payload.enabled ?? true,
    timeout_seconds: payload.timeoutSeconds ?? 20,
    use_saved_secret: payload.useSavedSecret ?? false,
  };
  if (payload.capabilityChecks && payload.capabilityChecks.length > 0) {
    request.capability_checks = payload.capabilityChecks;
  }
  return request;
}

function toSnakeNotificationTestPayload(payload: TestNotificationChannelRequest): Record<string, unknown> {
  return {
    channel: payload.channel,
    items: (payload.items || []).map((item) => ({
      key: item.key,
      value: item.value,
    })),
    mask_token: payload.maskToken ?? '******',
    title: payload.title ?? 'DSA 通知测试',
    content: payload.content ?? '这是一条来自 DSA Web 设置页的通知测试消息。',
    timeout_seconds: payload.timeoutSeconds ?? 20,
  };
}

function toSnakeDiscoverModelsPayload(payload: DiscoverLLMChannelModelsRequest): Record<string, unknown> {
  return {
    name: payload.name,
    protocol: payload.protocol,
    base_url: payload.baseUrl ?? '',
    api_key: payload.apiKey ?? '',
    models: payload.models,
    timeout_seconds: payload.timeoutSeconds ?? 20,
    use_saved_secret: payload.useSavedSecret ?? false,
  };
}

function toSnakeGenerationBackendStatusPreviewPayload(
  payload: GenerationBackendStatusPreviewRequest = {},
): Record<string, unknown> {
  return {
    items: (payload.items || []).map((item) => ({
      key: item.key,
      value: item.value,
    })),
    mask_token: payload.maskToken ?? '******',
  };
}

function toSnakeGenerationBackendSmokePayload(payload: TestGenerationBackendRequest = {}): Record<string, unknown> {
  const request: Record<string, unknown> = {
    mode: payload.mode ?? 'json',
    items: (payload.items || []).map((item) => ({
      key: item.key,
      value: item.value,
    })),
    mask_token: payload.maskToken ?? '******',
  };
  if (payload.backendId) {
    request.backend_id = payload.backendId;
  }
  if (payload.timeoutSeconds !== undefined && payload.timeoutSeconds !== null) {
    request.timeout_seconds = payload.timeoutSeconds;
  }
  return request;
}

function toSnakeAgentBackendPayload(
  payload: AgentBackendStatusPreviewRequest = {},
): Record<string, unknown> {
  return {
    items: (payload.items || []).map((item) => ({ key: item.key, value: item.value })),
    mask_token: payload.maskToken ?? '******',
  };
}

export const systemConfigApi = {
  async getConfig(includeSchema = true): Promise<SystemConfigResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/system/config', {
      params: { include_schema: includeSchema },
    });
    return toCamelCase<SystemConfigResponse>(response.data);
  },

  async exportEnv(): Promise<ExportSystemConfigResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/system/config/export');
    return toCamelCase<ExportSystemConfigResponse>(response.data);
  },

  async exportDesktopEnv(): Promise<ExportSystemConfigResponse> {
    return this.exportEnv();
  },

  async getSchema(): Promise<SystemConfigSchemaResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/system/config/schema');
    return toCamelCase<SystemConfigSchemaResponse>(response.data);
  },

  async getSetupStatus(): Promise<SetupStatusResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/system/config/setup/status');
    return toCamelCase<SetupStatusResponse>(response.data);
  },

  async getGenerationBackendStatus(): Promise<GenerationBackendStatusResponse> {
    const response = await apiClient.get<Record<string, unknown>>(
      '/api/v1/system/config/generation-backends/status',
    );
    return toCamelCase<GenerationBackendStatusResponse>(response.data);
  },

  async previewGenerationBackendStatus(
    payload: GenerationBackendStatusPreviewRequest = {},
  ): Promise<GenerationBackendStatusResponse> {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/system/config/generation-backends/status/preview',
      toSnakeGenerationBackendStatusPreviewPayload(payload),
    );
    return toCamelCase<GenerationBackendStatusResponse>(response.data);
  },

  async testGenerationBackend(payload: TestGenerationBackendRequest = {}): Promise<TestGenerationBackendResponse> {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/system/config/generation-backends/smoke-test',
      toSnakeGenerationBackendSmokePayload(payload),
    );
    return toCamelCase<TestGenerationBackendResponse>(response.data);
  },

  async getAgentBackendStatus(): Promise<AgentBackendStatusResponse> {
    const response = await apiClient.get<Record<string, unknown>>(
      '/api/v1/system/config/agent-backends/status',
    );
    return toCamelCase<AgentBackendStatusResponse>(response.data);
  },

  async previewAgentBackendStatus(
    payload: AgentBackendStatusPreviewRequest = {},
  ): Promise<AgentBackendStatusResponse> {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/system/config/agent-backends/status/preview',
      toSnakeAgentBackendPayload(payload),
    );
    return toCamelCase<AgentBackendStatusResponse>(response.data);
  },

  async getSchedulerStatus(): Promise<SchedulerStatusResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/system/scheduler/status');
    return toCamelCase<SchedulerStatusResponse>(response.data);
  },

  async runSchedulerNow(): Promise<SchedulerRunNowResponse> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/system/scheduler/run-now');
    return toCamelCase<SchedulerRunNowResponse>(response.data);
  },

  async validate(payload: ValidateSystemConfigRequest): Promise<ValidateSystemConfigResponse> {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/system/config/validate',
      toSnakeValidatePayload(payload),
    );
    return toCamelCase<ValidateSystemConfigResponse>(response.data);
  },

  async importEnv(payload: ImportSystemConfigRequest): Promise<UpdateSystemConfigResponse> {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/system/config/import',
      toSnakeImportPayload(payload),
    );
    return toCamelCase<UpdateSystemConfigResponse>(response.data);
  },

  async importDesktopEnv(payload: ImportSystemConfigRequest): Promise<UpdateSystemConfigResponse> {
    return this.importEnv(payload);
  },

  async testLLMChannel(payload: TestLLMChannelRequest): Promise<TestLLMChannelResponse> {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/system/config/llm/test-channel',
      toSnakeTestChannelPayload(payload),
    );
    return toCamelCase<TestLLMChannelResponse>(response.data);
  },

  async testNotificationChannel(payload: TestNotificationChannelRequest): Promise<TestNotificationChannelResponse> {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/system/config/notification/test-channel',
      toSnakeNotificationTestPayload(payload),
    );
    return toCamelCase<TestNotificationChannelResponse>(response.data);
  },

  async discoverLLMChannelModels(
    payload: DiscoverLLMChannelModelsRequest,
  ): Promise<DiscoverLLMChannelModelsResponse> {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/system/config/llm/discover-models',
      toSnakeDiscoverModelsPayload(payload),
    );
    return toCamelCase<DiscoverLLMChannelModelsResponse>(response.data);
  },

  async update(payload: UpdateSystemConfigRequest): Promise<UpdateSystemConfigResponse> {
    try {
      const response = await apiClient.put<Record<string, unknown>>(
        '/api/v1/system/config',
        toSnakeUpdatePayload(payload),
      );
      return toCamelCase<UpdateSystemConfigResponse>(response.data);
    } catch (error: unknown) {
      const parsed = getParsedApiError(error);
      if (error && typeof error === 'object' && 'response' in error) {
        const status = (error as { response?: { status?: number } }).response?.status;
        const payloadData = (error as { response?: { data?: unknown } }).response?.data;

        if (status === 400) {
          const validationError = toCamelCase<SystemConfigValidationErrorResponse>(payloadData ?? {});
          throw new SystemConfigValidationError(
            parsed.message || validationError.message || '配置校验失败',
            validationError.issues || [],
            parsed,
          );
        }

        if (status === 409) {
          const conflict = toCamelCase<SystemConfigConflictResponse>(payloadData ?? {});
          throw new SystemConfigConflictError(
            parsed.message || conflict.message || '配置版本冲突',
            conflict.currentConfigVersion,
            parsed,
          );
        }
      }

      throw error;
    }
  },

  /**
   * 获取自选队列股票代码列表
   */
  getWatchlist: async (): Promise<string[]> => {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/stocks/watchlist');
    const data = toCamelCase<{ stockCodes: string[] }>(response.data);
    return data.stockCodes || [];
  },

  /**
   * 添加股票到自选队列
   */
  addToWatchlist: async (stockCode: string): Promise<string[]> => {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/stocks/watchlist/add', {
      stock_code: stockCode,
    });
    const data = toCamelCase<{ stockCodes: string[] }>(response.data);
    return data.stockCodes || [];
  },

  /**
   * 从自选队列移除股票
   */
  removeFromWatchlist: async (stockCode: string): Promise<string[]> => {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/stocks/watchlist/remove', {
      stock_code: stockCode,
    });
    const data = toCamelCase<{ stockCodes: string[] }>(response.data);
    return data.stockCodes || [];
  },

  /**
   * 批量获取自选股的实时行情（含量比），用于首页放量预警。
   * 服务端使用 ThreadPoolExecutor 并发拉取，单股失败不影响其它股票。
   *
   * 返回 Map<code, { volumeRatio, changePercent, amount, error? }>。
   */
  fetchWatchlistSpotQuotes: async (
    codes: string[],
  ): Promise<Map<string, WatchlistSpotQuoteView>> => {
    const result = new Map<string, WatchlistSpotQuoteView>();
    if (!codes.length) return result;
    // 分批调用：后端 WatchlistSpotQuotesRequest.codes 有长度上限（默认 300），
    // 历史栏常 >50 只，单次超限会 422 被当“无数据”清空；分批也避免大列表单次
    // 请求超过 axios 30s 超时。批大小取 40，留足余量。
    const BATCH_SIZE = 40;
    const uniqueCodes = Array.from(new Set(codes));
    try {
      for (let i = 0; i < uniqueCodes.length; i += BATCH_SIZE) {
        const chunk = uniqueCodes.slice(i, i + BATCH_SIZE);
        const response = await apiClient.post<Record<string, unknown>>(
          '/api/v1/stocks/watchlist/spot-quotes',
          { codes: chunk },
        );
        const data = toCamelCase<{
          quotes: Array<{
            stockCode: string;
            quote: WatchlistQuotePayload | null;
            error: string | null;
          }>;
        }>(response.data);
        for (const item of data.quotes || []) {
          if (!item || !item.stockCode) continue;
          // Use a normalized key so the Map can be looked up by either the
          // raw backend value (``000966.SZ``) or any other canonicalization
          // the caller passes through ``HomePage.getStockCodeKey()``.
          const key = normalizeStockCode(item.stockCode).toUpperCase();
          if (!key) continue;
          result.set(key, {
            volumeRatio: item.quote?.volumeRatio ?? null,
            changePercent: item.quote?.changePercent ?? null,
            amount: item.quote?.amount ?? null,
            error: item.error ?? null,
          });
        }
      }
    } catch (error) {
      // 不再静默吞掉：整批/分片请求失败（网络/超时/校验错误）时向上抛出，
      // 由调用方保留上次成功的数据，避免被空 Map 覆盖导致全部显示「暂无」。
      console.error('批量获取自选股实时行情失败:', error);
      throw error;
    }
    return result;
  },
};
