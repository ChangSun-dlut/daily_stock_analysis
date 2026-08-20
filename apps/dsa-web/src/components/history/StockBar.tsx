import type React from 'react';
import { useState, useCallback, useRef, useEffect, useId } from 'react';
import { Badge, Button, ScrollArea } from '../common';
import { DashboardPanelHeader, DashboardStateBlock } from '../dashboard';
import { StockBarItemComponent } from './StockBarItem';
import type { StockBarItem as StockBarItemType } from '../../types/analysis';
import type { WatchlistSpotQuoteView } from '../../api/systemConfig';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import { Zap, Loader2 } from 'lucide-react';

interface StockBarProps {
  items: StockBarItemType[];
  isLoading: boolean;
  selectedStockCode?: string;
  selectedRecordId?: number;
  onItemClick: (recordId: number) => void;
  onDeleteStock?: (stockCode: string) => Promise<void> | void;
  isDeleting?: boolean;
  /**
   * Bubble the currently selected recordIds up so siblings (e.g. a batch
   * share-image button rendered elsewhere on the page) can react.
   */
  onSelectionChange?: (recordIds: number[]) => void;
  /** 实时量比 / 涨跌幅映射，用于在每行渲染放量预警徽标（与 watchlist 复用同一来源）。 */
  spotQuotesByCode?: Map<string, WatchlistSpotQuoteView>;
  spotQuotesLoading?: boolean;
  /** 手动触发历史栏目实时量比刷新（闪电按钮）。 */
  onRefreshSpotQuotes?: () => Promise<void> | void;
  className?: string;
}

/**
 * 个股栏组件：以股票维度展示历史分析记录，每只股票只显示一条。
 * 大盘复盘可作为 MARKET 项参与展示，并按最近分析时间排序。
 */
export const StockBar: React.FC<StockBarProps> = ({
  items,
  isLoading,
  selectedStockCode,
  selectedRecordId,
  onItemClick,
  onDeleteStock,
  isDeleting = false,
  onSelectionChange,
  spotQuotesByCode,
  spotQuotesLoading = false,
  onRefreshSpotQuotes,
  className = '',
}) => {
  const { t } = useUiLanguage();
  const isMarketReview = (code: string) => code === 'MARKET';
  const [selectedCodes, setSelectedCodes] = useState<Set<string>>(new Set());
  const selectAllRef = useRef<HTMLInputElement>(null);
  const selectAllId = useId();

  const deletableItems = items;
  const selectedCount = [...selectedCodes].filter((code) => deletableItems.some((item) => item.stockCode === code)).length;
  const allVisibleSelected = deletableItems.length > 0 && selectedCount === deletableItems.length;
  const someVisibleSelected = selectedCount > 0 && !allVisibleSelected;

  useEffect(() => {
    if (selectAllRef.current) {
      selectAllRef.current.indeterminate = someVisibleSelected;
    }
  }, [someVisibleSelected]);

  // Bubble selected recordIds up so other UI (e.g. the batch share button)
  // can read them without lifting the selection state to a global store.
  useEffect(() => {
    if (!onSelectionChange) return;
    const recordIds: number[] = [];
    for (const code of selectedCodes) {
      if (code === 'MARKET') continue;
      const item = items.find((it) => it.stockCode === code);
      if (item && typeof item.id === 'number') {
        recordIds.push(item.id);
      }
    }
    onSelectionChange(recordIds);
  }, [selectedCodes, items, onSelectionChange]);

  const toggleCode = useCallback((code: string) => {
    setSelectedCodes((prev) => {
      const next = new Set(prev);
      if (next.has(code)) next.delete(code);
      else next.add(code);
      return next;
    });
  }, []);

  const toggleSelectAll = useCallback(() => {
    setSelectedCodes((prev) => {
      if (prev.size === deletableItems.length) return new Set();
      return new Set(deletableItems.map((item) => item.stockCode));
    });
  }, [deletableItems]);

  const handleDeleteSelected = useCallback(async () => {
    if (!onDeleteStock || selectedCodes.size === 0) return;
    const codesToDelete = [...selectedCodes];
    for (const code of codesToDelete) {
      await onDeleteStock(code);
    }
    setSelectedCodes(new Set());
  }, [onDeleteStock, selectedCodes]);

  return (
    <aside
      data-testid="home-stock-bar"
      className={`glass-card home-stock-scroll-shell flex min-h-0 flex-col ${className}`}
    >
      <ScrollArea
        viewportClassName="p-4 overscroll-y-contain touch-pan-y"
        testId="home-stock-bar-scroll"
      >
        <div className="mb-4 space-y-3">
          <DashboardPanelHeader
            className="mb-1"
            title={t('stockBar.title')}
            titleClassName="text-sm font-medium"
            leading={(
              <svg className="h-4 w-4 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
              </svg>
            )}
            headingClassName="items-center"
            actions={
              selectedCount > 0 ? (
                <Badge variant="info" size="sm" className="animate-in fade-in zoom-in duration-200">
                  {t('common.selectedCount', { count: selectedCount })}
                </Badge>
              ) : items.length > 0 ? (
                <div className="flex items-center gap-1.5">
                  <span className="text-[11px] text-muted-text">{t('common.itemsCount', { count: items.length })}</span>
                  {onRefreshSpotQuotes ? (
                    <Button
                      type="button"
                      variant="ghost"
                      size="xsm"
                      className="h-7 w-7 px-0"
                      disabled={spotQuotesLoading}
                      onClick={() => void onRefreshSpotQuotes()}
                      aria-label={t('history.refreshSpotQuotesAria')}
                      title={t('history.refreshSpotQuotesAria')}
                    >
                      {spotQuotesLoading ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                      ) : (
                        <Zap className="h-3.5 w-3.5" aria-hidden="true" />
                      )}
                    </Button>
                  ) : null}
                </div>
              ) : undefined
            }
          />

          {items.length > 0 && onDeleteStock && (
            <div className="flex items-center gap-2">
              <label
                className="flex flex-1 cursor-pointer items-center gap-2 rounded-lg px-2 py-1"
                htmlFor={selectAllId}
              >
                <input
                  id={selectAllId}
                  ref={selectAllRef}
                  type="checkbox"
                  checked={allVisibleSelected}
                  onChange={toggleSelectAll}
                  disabled={isDeleting}
                  aria-label={t('history.selectAllStockAria')}
                  className="h-3.5 w-3.5 cursor-pointer bg-transparent accent-primary focus:ring-primary/30 disabled:opacity-50"
                />
                <span className="text-[11px] text-muted-text select-none">{t('common.selectAllCurrent')}</span>
              </label>
              <Button
                variant="danger-subtle"
                size="xsm"
                onClick={() => void handleDeleteSelected()}
                disabled={selectedCount === 0 || isDeleting}
                isLoading={isDeleting}
                className="disabled:!border-transparent disabled:!bg-transparent"
              >
                {isDeleting ? t('common.deleting') : t('common.delete')}
              </Button>
            </div>
          )}
        </div>

        {isLoading ? (
          <DashboardStateBlock
            loading
            compact
            title={t('stockBar.loading')}
          />
        ) : items.length === 0 ? (
          <DashboardStateBlock
            title={t('stockBar.emptyTitle')}
            description={t('stockBar.emptyDescription')}
            icon={(
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
            )}
          />
        ) : (
          <div className="space-y-1.5">
            {items.map((item) => {
              const code = item.stockCode || '';
              const isMarket = isMarketReview(code);
              const isSelected = selectedRecordId === item.id || selectedStockCode === code;
              const isChecked = selectedCodes.has(code);
              const spotQuote = spotQuotesByCode?.get(code);
              const isVolumeLoading = !spotQuote && spotQuotesLoading;

              return (
                <div key={`${code}-${item.id}`} className="flex items-start gap-2 group">
                  {onDeleteStock && (
                    <div className="pt-5">
                      <input
                        type="checkbox"
                        checked={isChecked}
                        onChange={() => toggleCode(code)}
                        disabled={isDeleting}
                        className="h-3.5 w-3.5 cursor-pointer rounded border-subtle-hover bg-transparent accent-primary focus:ring-primary/30 disabled:opacity-50"
                      />
                    </div>
                  )}
                  <StockBarItemComponent
                    item={item}
                    isViewing={isSelected}
                    onClick={onItemClick}
                    onDelete={onDeleteStock}
                    isDeleting={isDeleting}
                    isMarketReview={isMarket}
                    volumeRatio={spotQuote?.volumeRatio ?? null}
                    volumeChangePercent={spotQuote?.changePercent ?? null}
                    isVolumeRatioLoading={isVolumeLoading}
                  />
                </div>
              );
            })}
          </div>
        )}
      </ScrollArea>
    </aside>
  );
};
