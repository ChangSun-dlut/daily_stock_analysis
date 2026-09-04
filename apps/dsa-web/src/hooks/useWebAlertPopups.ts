import { useCallback, useEffect, useRef, useState } from 'react';
import { alertsApi } from '../api/alerts';
import type { WebPopupItem } from '../types/alerts';

type UseWebAlertPopupsOptions = {
  /** Poll interval in milliseconds. Set to 0 to disable polling. Default 5s. */
  intervalMs?: number;
  /** Hide alerts older than this on first load. Default 24h. */
  maxAgeMs?: number;
  /** When true, no polling and no fetching happens. */
  paused?: boolean;
};

const DEFAULT_INTERVAL_MS = 5000;
const DEFAULT_MAX_AGE_MS = 24 * 60 * 60 * 1000;
const MAX_HISTORY = 50;

/**
 * Polls the backend `/api/v1/alerts/web-popups` endpoint and surfaces new
 * alerts as they arrive. Acts as a fallback delivery channel when the primary
 * push channel (WeChat/Feishu) is unavailable or slow.
 *
 * The hook is intentionally generic — components decide how to render each
 * popup (toast, modal, banner, etc.) using `pendingItems`.
 */
export function useWebAlertPopups(options: UseWebAlertPopupsOptions = {}) {
  const { intervalMs = DEFAULT_INTERVAL_MS, maxAgeMs = DEFAULT_MAX_AGE_MS, paused = false } = options;
  const [history, setHistory] = useState<WebPopupItem[]>([]);
  const [activeItems, setActiveItems] = useState<WebPopupItem[]>([]);
  const [latestId, setLatestId] = useState(0);
  const latestIdRef = useRef(0);
  const timerRef = useRef<number | null>(null);

  const addItems = useCallback((items: WebPopupItem[]) => {
    if (items.length === 0) return;
    setHistory((prev) => {
      const merged = new Map<number, WebPopupItem>();
      prev.forEach((i) => merged.set(i.id, i));
      items.forEach((i) => merged.set(i.id, i));
      return Array.from(merged.values()).slice(-MAX_HISTORY);
    });
    setActiveItems((prev) => {
      const existing = new Set(prev.map((i) => i.id));
      const added = items.filter((i) => !existing.has(i.id));
      return [...prev, ...added];
    });
  }, []);

  const fetchOnce = useCallback(async () => {
    try {
      const data = await alertsApi.listWebPopups(latestIdRef.current);
      const items = data.items ?? [];
      if (items.length === 0) {
        return;
      }
      // Filter out items older than maxAgeMs on the very first fetch so we
      // don't replay the whole ring buffer on page load.
      const cutoff = Date.now() - maxAgeMs;
      const fresh = items.filter((item) => {
        const ts = Date.parse(item.createdAt);
        return Number.isFinite(ts) ? ts >= cutoff : true;
      });
      if (fresh.length === 0) {
        latestIdRef.current = Math.max(latestIdRef.current, data.latestId);
        return;
      }
      latestIdRef.current = Math.max(latestIdRef.current, data.latestId);
      setLatestId(latestIdRef.current);
      addItems(fresh);
    } catch (err) {
      // Network blips are expected; just swallow and try again next tick.
      console.debug('[web-alerts] poll failed', err);
    }
  }, [maxAgeMs, addItems]);

  useEffect(() => {
    if (paused || intervalMs <= 0) {
      return undefined;
    }
    // Defer the first fetch off the effect body so subsequent setState
    // (driven by the polling loop) doesn't fire synchronously inside the effect.
    const initialTimer = window.setTimeout(() => {
      void fetchOnce();
    }, 0);
    timerRef.current = window.setInterval(() => {
      void fetchOnce();
    }, intervalMs);
    return () => {
      window.clearTimeout(initialTimer);
      if (timerRef.current !== null) {
        window.clearInterval(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [paused, intervalMs, fetchOnce]);

  /** Dismiss a popup from the active toast stack only (history is preserved). */
  const dismiss = useCallback((id: number) => {
    setActiveItems((prev) => prev.filter((item) => item.id !== id));
  }, []);

  /** Remove one item from the notification center history (and active stack if still shown). */
  const removeHistory = useCallback((id: number) => {
    setHistory((prev) => prev.filter((item) => item.id !== id));
    setActiveItems((prev) => prev.filter((item) => item.id !== id));
  }, []);

  const clear = useCallback(() => {
    setHistory([]);
    setActiveItems([]);
  }, []);

  return { history, activeItems, dismiss, removeHistory, clear, latestId };
}