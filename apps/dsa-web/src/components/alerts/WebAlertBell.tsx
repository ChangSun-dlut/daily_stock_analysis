import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Bell, Monitor, X } from 'lucide-react';
import type { WebPopupItem } from '../../types/alerts';
import { cn } from '../../utils/cn';

const LAST_READ_KEY = 'dsa:last-read-alert-id';
const DESKTOP_ENABLED_KEY = 'dsa:desktop-notifications-enabled';
const SOUND_ENABLED_KEY = 'dsa:alert-sound-enabled';

function formatRelativeTime(iso: string): string {
  const ts = Date.parse(iso);
  if (!Number.isFinite(ts)) {
    return '';
  }
  const diffSec = Math.round((Date.now() - ts) / 1000);
  if (diffSec < 5) return '刚刚';
  if (diffSec < 60) return `${diffSec} 秒前`;
  const diffMin = Math.round(diffSec / 60);
  if (diffMin < 60) return `${diffMin} 分钟前`;
  const diffHour = Math.round(diffMin / 60);
  if (diffHour < 24) return `${diffHour} 小时前`;
  const date = new Date(ts);
  const pad = (n: number) => n.toString().padStart(2, '0');
  return `${date.getMonth() + 1}/${date.getDate()} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function playAlertSound() {
  try {
    const AudioCtx = (window as any).AudioContext || (window as any).webkitAudioContext;
    if (!AudioCtx) return;
    const ctx = new AudioCtx();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.type = 'sine';
    osc.frequency.setValueAtTime(880, ctx.currentTime);
    osc.frequency.exponentialRampToValueAtTime(440, ctx.currentTime + 0.15);
    gain.gain.setValueAtTime(0.08, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.25);
    osc.start();
    osc.stop(ctx.currentTime + 0.25);
  } catch {
    // ignore
  }
}

function showDesktopNotification(item: WebPopupItem) {
  if (typeof window === 'undefined' || !('Notification' in window)) return;
  if (!document.hidden || Notification.permission !== 'granted') return;
  try {
    new Notification(item.title || '预警提醒', {
      body: item.body ? item.body.slice(0, 220) : '',
      tag: `dsa-alert-${item.id}`,
      requireInteraction: true,
    });
  } catch {
    // ignore
  }
}

interface WebAlertBellProps {
  historyItems: WebPopupItem[];
  dismiss: (id: number) => void;
  clear: () => void;
  latestId: number;
  onOpenChange?: (open: boolean) => void;
}

const CollapsibleAlertItem: React.FC<{
  item: WebPopupItem;
  onDismiss: (id: number) => void;
}> = ({ item, onDismiss }) => {
  const [expanded, setExpanded] = useState(false);
  const shouldCollapse = item.body.length > 120;
  const displayBody = expanded || !shouldCollapse ? item.body : `${item.body.slice(0, 120).trim()}…`;

  return (
    <div
      className="group relative border-b border-border/30 px-4 py-3 transition-colors hover:bg-hover/50"
      onClick={() => setExpanded((prev) => !prev)}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          setExpanded((prev) => !prev);
        }
      }}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium text-foreground">{item.title}</div>
          <pre
            className={cn(
              'mt-1 whitespace-pre-wrap break-words font-sans text-xs leading-relaxed text-secondary-text',
              !expanded && shouldCollapse && 'line-clamp-4'
            )}
          >
            {displayBody}
          </pre>
          {shouldCollapse && (
            <div className="mt-1 text-[10px] text-sky-300/80">
              {expanded ? '点击收起' : '点击展开'}
            </div>
          )}
          <div className="mt-1 text-[10px] text-secondary-text/60">
            {formatRelativeTime(item.createdAt)}
          </div>
        </div>
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onDismiss(item.id);
          }}
          className="shrink-0 rounded-md p-1 text-secondary-text/60 opacity-0 transition-opacity hover:bg-hover hover:text-foreground group-hover:opacity-100"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
    </div>
  );
};

export const WebAlertBell: React.FC<WebAlertBellProps> = ({
  historyItems,
  dismiss,
  clear,
  latestId,
  onOpenChange,
}) => {
  const [open, setOpen] = useState(false);
  const [lastReadId, setLastReadId] = useState(() => {
    const raw = typeof window !== 'undefined' ? localStorage.getItem(LAST_READ_KEY) : null;
    return raw ? parseInt(raw, 10) || 0 : 0;
  });
  const [desktopEnabled, setDesktopEnabled] = useState(() => {
    if (typeof window === 'undefined') return false;
    return (
      localStorage.getItem(DESKTOP_ENABLED_KEY) === 'true' &&
      'Notification' in window &&
      Notification.permission === 'granted'
    );
  });
  const [soundEnabled, setSoundEnabled] = useState(() => {
    return typeof window !== 'undefined' && localStorage.getItem(SOUND_ENABLED_KEY) === 'true';
  });
  const containerRef = useRef<HTMLDivElement>(null);
  const prevIdsRef = useRef<Set<number>>(new Set());

  // Sound + desktop notification for truly new items.
  useEffect(() => {
    const currentIds = new Set(historyItems.map((i) => i.id));
    const newItems = historyItems.filter((i) => !prevIdsRef.current.has(i.id));
    if (newItems.length > 0) {
      if (soundEnabled) {
        playAlertSound();
      }
      newItems.forEach(showDesktopNotification);
    }
    prevIdsRef.current = currentIds;
  }, [historyItems, soundEnabled]);

  // Close dropdown on outside click.
  useEffect(() => {
    if (!open) return undefined;
    const handle = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
        onOpenChange?.(false);
      }
    };
    document.addEventListener('mousedown', handle);
    return () => document.removeEventListener('mousedown', handle);
  }, [open, onOpenChange]);

  const unreadCount = historyItems.filter((i) => i.id > lastReadId).length;

  const markAllRead = useCallback(() => {
    const id = latestId > 0 ? latestId : lastReadId;
    if (id > lastReadId) {
      setLastReadId(id);
      localStorage.setItem(LAST_READ_KEY, String(id));
    }
  }, [latestId, lastReadId]);

  const handleToggle = useCallback(() => {
    setOpen((prev) => {
      const next = !prev;
      if (next) {
        markAllRead();
      }
      onOpenChange?.(next);
      return next;
    });
  }, [markAllRead, onOpenChange]);

  const requestDesktop = useCallback(async () => {
    if (typeof window === 'undefined' || !('Notification' in window)) return;
    const perm = await Notification.requestPermission();
    const enabled = perm === 'granted';
    setDesktopEnabled(enabled);
    localStorage.setItem(DESKTOP_ENABLED_KEY, String(enabled));
  }, []);

  const toggleSound = useCallback(() => {
    setSoundEnabled((prev) => {
      const next = !prev;
      localStorage.setItem(SOUND_ENABLED_KEY, String(next));
      return next;
    });
  }, []);

  const visibleItems = historyItems.slice(-20).reverse();

  return (
    <div ref={containerRef} className="relative">
      <button
        type="button"
        onClick={handleToggle}
        aria-label="消息通知"
        className={cn(
          'relative flex h-10 w-10 items-center justify-center rounded-xl border border-border/70 bg-card/85 text-secondary-text shadow-soft-card backdrop-blur-md transition-colors hover:bg-hover hover:text-foreground',
          open && 'bg-hover text-foreground'
        )}
      >
        <Bell className="h-5 w-5" />
        {unreadCount > 0 && (
          <span className="absolute -right-1 -top-1 flex h-5 min-w-5 items-center justify-center rounded-full bg-rose-500 px-1 text-[10px] font-bold text-white shadow-sm">
            {unreadCount > 99 ? '99+' : unreadCount}
          </span>
        )}
      </button>

      {open && (
        <div className="absolute right-0 top-12 z-50 w-[360px] max-w-[calc(100vw-32px)] overflow-hidden rounded-2xl border border-border/70 bg-card/95 shadow-soft-card backdrop-blur-xl">
          <div className="flex items-center justify-between border-b border-border/50 px-4 py-3">
            <span className="text-sm font-semibold text-foreground">通知中心</span>
            <div className="flex items-center gap-1">
              <button
                type="button"
                onClick={toggleSound}
                title={soundEnabled ? '关闭提示音' : '开启提示音'}
                className={cn(
                  'rounded-md p-1.5 text-xs transition-colors hover:bg-hover',
                  soundEnabled ? 'text-amber-400' : 'text-secondary-text'
                )}
              >
                {soundEnabled ? '🔔' : '🔕'}
              </button>
              <button
                type="button"
                onClick={requestDesktop}
                title={desktopEnabled ? '桌面通知已开启' : '开启桌面通知'}
                className={cn(
                  'rounded-md p-1.5 transition-colors hover:bg-hover',
                  desktopEnabled ? 'text-emerald-400' : 'text-secondary-text'
                )}
              >
                <Monitor className="h-4 w-4" />
              </button>
              {historyItems.length > 0 && (
                <button
                  type="button"
                  onClick={() => {
                    clear();
                    markAllRead();
                  }}
                  title="清空"
                  className="rounded-md p-1.5 text-secondary-text transition-colors hover:bg-hover hover:text-foreground"
                >
                  <X className="h-4 w-4" />
                </button>
              )}
            </div>
          </div>

          {!desktopEnabled && typeof window !== 'undefined' && 'Notification' in window && (
            <div className="border-b border-border/50 bg-amber-500/10 px-4 py-2 text-xs text-amber-200">
              建议开启桌面通知，切到后台时也能收到预警。
            </div>
          )}

          <div className="max-h-[60vh] overflow-y-auto">
            {visibleItems.length === 0 ? (
              <div className="px-4 py-8 text-center text-sm text-secondary-text">暂无通知</div>
            ) : (
              visibleItems.map((item) => <CollapsibleAlertItem key={item.id} item={item} onDismiss={dismiss} />)
            )}
          </div>
        </div>
      )}
    </div>
  );
};
