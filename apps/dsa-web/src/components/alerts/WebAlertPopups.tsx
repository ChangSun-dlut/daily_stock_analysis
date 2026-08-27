import type React from 'react';
import { useEffect, useState } from 'react';
import { AlertTriangle, Bell, CheckCircle2, Info, X } from 'lucide-react';
import type { WebPopupItem, WebPopupLevel } from '../../types/alerts';
import { cn } from '../../utils/cn';

interface WebAlertPopupsProps {
  items: WebPopupItem[];
  onDismiss: (id: number) => void;
  /** Auto-dismiss each popup after this many ms. Default 12s. */
  autoCloseMs?: number;
}

const LEVEL_STYLES: Record<
  WebPopupLevel,
  {
    container: string;
    iconWrap: string;
    iconClass: string;
    titleClass: string;
    bodyClass: string;
  }
> = {
  warning: {
    container: 'border-amber-500/40 bg-amber-500/10',
    iconWrap: 'bg-amber-500/20 text-amber-300',
    iconClass: 'text-amber-300',
    titleClass: 'text-amber-100',
    bodyClass: 'text-amber-50/85',
  },
  error: {
    container: 'border-rose-500/40 bg-rose-500/10',
    iconWrap: 'bg-rose-500/20 text-rose-300',
    iconClass: 'text-rose-300',
    titleClass: 'text-rose-100',
    bodyClass: 'text-rose-50/85',
  },
  success: {
    container: 'border-emerald-500/40 bg-emerald-500/10',
    iconWrap: 'bg-emerald-500/20 text-emerald-300',
    iconClass: 'text-emerald-300',
    titleClass: 'text-emerald-100',
    bodyClass: 'text-emerald-50/85',
  },
  info: {
    container: 'border-sky-500/40 bg-sky-500/10',
    iconWrap: 'bg-sky-500/20 text-sky-300',
    iconClass: 'text-sky-300',
    titleClass: 'text-sky-100',
    bodyClass: 'text-sky-50/85',
  },
};

const LevelIcon: React.FC<{ level: WebPopupLevel }> = ({ level }) => {
  if (level === 'error') return <AlertTriangle className="h-4 w-4" />;
  if (level === 'warning') return <AlertTriangle className="h-4 w-4" />;
  if (level === 'success') return <CheckCircle2 className="h-4 w-4" />;
  return <Info className="h-4 w-4" />;
};

const PopupCard: React.FC<{
  item: WebPopupItem;
  onDismiss: (id: number) => void;
  autoCloseMs: number;
}> = ({ item, onDismiss, autoCloseMs }) => {
  const styles = LEVEL_STYLES[item.level] ?? LEVEL_STYLES.warning;
  const [paused, setPaused] = useState(false);

  useEffect(() => {
    if (autoCloseMs <= 0) {
      return undefined;
    }
    if (paused) {
      return undefined;
    }
    const handle = window.setTimeout(() => onDismiss(item.id), autoCloseMs);
    return () => window.clearTimeout(handle);
  }, [autoCloseMs, item.id, onDismiss, paused]);

  return (
    <div
      role="alertdialog"
      aria-live="polite"
      className={cn(
        'pointer-events-auto relative w-[360px] max-w-[calc(100vw-32px)] overflow-hidden rounded-2xl border backdrop-blur-md shadow-soft-card transition-all',
        'animate-[fade-in-up_200ms_ease-out]',
        styles.container,
      )}
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocus={() => setPaused(true)}
      onBlur={() => setPaused(false)}
    >
      <div className="flex items-start gap-3 px-4 py-3">
        <div className={cn('mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full', styles.iconWrap)}>
          <LevelIcon level={item.level} />
        </div>
        <div className="min-w-0 flex-1">
          <div className={cn('flex items-center gap-2 text-sm font-semibold leading-tight', styles.titleClass)}>
            <Bell className={cn('h-3.5 w-3.5 opacity-80', styles.iconClass)} />
            <span className="truncate">{item.title || '预警提醒'}</span>
          </div>
          {item.body ? (
            <pre
              className={cn(
                'mt-1.5 whitespace-pre-wrap break-words font-sans text-xs leading-relaxed',
                styles.bodyClass,
              )}
            >
              {item.body}
            </pre>
          ) : null}
          <div className="mt-1 text-[10px] uppercase tracking-wide text-secondary-text/70">
            {formatRelativeTime(item.createdAt)}
          </div>
        </div>
        <button
          type="button"
          aria-label="关闭"
          onClick={() => onDismiss(item.id)}
          className="ml-1 rounded-md p-1 text-secondary-text/80 transition-colors hover:bg-hover hover:text-foreground"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
    </div>
  );
};

/**
 * Stacked toast viewport that surfaces real-time alert popups served by the
 * backend `/api/v1/alerts/web-popups` endpoint. Designed as a fallback delivery
 * channel when the primary push (e.g. WeChat) is unavailable.
 */
export const WebAlertPopups: React.FC<WebAlertPopupsProps> = ({
  items,
  onDismiss,
  autoCloseMs = 12000,
}) => {
  if (items.length === 0) {
    return null;
  }
  return (
    <div className="pointer-events-none fixed bottom-5 right-5 z-50 flex max-h-[calc(100vh-40px)] w-[360px] max-w-[calc(100vw-32px)] flex-col gap-3 overflow-y-auto">
      {items.map((item) => (
        <PopupCard key={item.id} item={item} onDismiss={onDismiss} autoCloseMs={autoCloseMs} />
      ))}
    </div>
  );
};

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