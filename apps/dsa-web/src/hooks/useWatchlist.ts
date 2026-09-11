import { useCallback, useEffect, useRef, useState } from 'react';
import { systemConfigApi } from '../api/systemConfig';
import { findMatchingStockCode, includesStockCode } from '../utils/stockCode';

export interface UseWatchlistReturn {
  watchlistCodes: string[];
  isLoading: boolean;
  isActioning: boolean;
  actionMessage: string | null;
  isInWatchlist: (stockCode: string) => boolean;
  addToWatchlist: (stockCode: string) => Promise<void>;
  removeFromWatchlist: (stockCode: string) => Promise<void>;
  toggleWatchlist: (stockCode: string) => Promise<void>;
  refresh: () => Promise<void>;
}

type WatchlistErrorPayload = {
  response?: { data?: { detail?: { message?: string } | string; message?: string } };
  message?: string;
};

/**
 * 把后端返回的真实错误透出给用户。
 *
 * 后端错误体形如 `{"error":"internal_error","message":"加入自选失败: ..."}` 或
 * `{"detail":{"error":"...","message":"..."}}`。只显示占位文案"操作失败"会让人
 * 完全无法自查（2026-09-08 的 LITELLM_FALLBACK_MODELS 校验失败就是这样被藏住的）。
 */
function extractErrorMessage(error: unknown, fallback: string): string {
  const payload = (error as WatchlistErrorPayload | null)?.response?.data;
  if (payload) {
    const detail = payload.detail;
    const detailMessage = typeof detail === 'string' ? detail : detail?.message;
    if (detailMessage) return detailMessage;
    if (payload.message) return payload.message;
  }
  const raw = (error as WatchlistErrorPayload | null)?.message;
  return raw && raw.trim() ? raw : fallback;
}

export function useWatchlist(): UseWatchlistReturn {
  const [codes, setCodes] = useState<string[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isActioning, setIsActioning] = useState(false);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const messageTimerRef = useRef<number | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (messageTimerRef.current !== null) {
        window.clearTimeout(messageTimerRef.current);
      }
    };
  }, []);

  const refresh = useCallback(async () => {
    try {
      const result = await systemConfigApi.getWatchlist();
      if (mountedRef.current) {
        setCodes(result);
      }
    } catch {
      // keep existing codes
    }
  }, []);

  useEffect(() => {
    setIsLoading(true);
    void refresh().finally(() => {
      if (mountedRef.current) {
        setIsLoading(false);
      }
    });
  }, [refresh]);

  const showMessage = useCallback((msg: string) => {
    if (messageTimerRef.current !== null) {
      window.clearTimeout(messageTimerRef.current);
    }
    setActionMessage(msg);
    messageTimerRef.current = window.setTimeout(() => {
      if (mountedRef.current) {
        setActionMessage(null);
      }
    }, 3000);
  }, []);

  const isInWatchlist = useCallback(
    (stockCode: string) => includesStockCode(codes, stockCode),
    [codes],
  );

  const addToWatchlist = useCallback(async (stockCode: string) => {
    if (!stockCode || isActioning) return;
    setIsActioning(true);
    try {
      const result = await systemConfigApi.addToWatchlist(stockCode);
      if (mountedRef.current) {
        setCodes(result);
        showMessage(`已加入自选 ${stockCode}`);
      }
    } catch (error: unknown) {
      if (mountedRef.current) showMessage(extractErrorMessage(error, '加入自选失败'));
    } finally {
      if (mountedRef.current) setIsActioning(false);
    }
  }, [isActioning, showMessage]);

  const removeFromWatchlist = useCallback(async (stockCode: string) => {
    if (!stockCode || isActioning) return;
    setIsActioning(true);
    try {
      const result = await systemConfigApi.removeFromWatchlist(stockCode);
      if (mountedRef.current) {
        setCodes(result);
        showMessage(`已从自选移除 ${stockCode}`);
      }
    } catch (error: unknown) {
      if (mountedRef.current) showMessage(extractErrorMessage(error, '移除自选失败'));
    } finally {
      if (mountedRef.current) setIsActioning(false);
    }
  }, [isActioning, showMessage]);

  const toggleWatchlist = useCallback(async (stockCode: string) => {
    const existingStockCode = findMatchingStockCode(codes, stockCode);
    if (existingStockCode) {
      await removeFromWatchlist(existingStockCode);
    } else {
      await addToWatchlist(stockCode);
    }
  }, [codes, removeFromWatchlist, addToWatchlist]);

  return {
    watchlistCodes: codes,
    isLoading,
    isActioning,
    actionMessage,
    isInWatchlist,
    addToWatchlist,
    removeFromWatchlist,
    toggleWatchlist,
    refresh,
  };
}
