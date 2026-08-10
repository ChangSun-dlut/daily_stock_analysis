import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { BarChart3, Clipboard, FileText, Gauge, Layers, ShieldAlert, TrendingUp, WalletCards, Workflow } from 'lucide-react';
import { historyApi } from '../../api/history';
import { formatUiText, UI_TEXT } from '../../i18n/uiText';
import type {
  AnalysisReport,
  MarketReviewPayload,
  MarketReviewPayloadSection,
  ReportLanguage,
} from '../../types/analysis';
import { markdownToPlainText } from '../../utils/markdown';
import { getReportText, normalizeReportLanguage } from '../../utils/reportLanguage';
import { Card } from '../common';
import { Tooltip } from '../common/Tooltip';
import { ReportMarkdownBody } from './ReportMarkdownBody';
import { ShareImageButton } from './ShareImageButton';

interface MarketReviewReportViewProps {
  report?: AnalysisReport;
  recordId?: number;
  content?: string;
  payload?: MarketReviewPayload | null;
  reportLanguage?: ReportLanguage;
  className?: string;
  onOpenRunFlow?: (recordId: number) => void;
}

type CopyType = 'markdown' | 'text';
type LoadedMarkdown = {
  recordId: number;
  content: string;
};
type LoadError = {
  recordId: number;
  message: string;
};
type MarketReviewSection = {
  id: string;
  title: string;
  content: string;
  icon: typeof FileText;
};
type StructuredMarketData = {
  id: string;
  title?: string;
  breadth?: MarketReviewPayload['breadth'];
  indices: NonNullable<MarketReviewPayload['indices']>;
  sectors?: MarketReviewPayload['sectors'];
  concepts?: MarketReviewPayload['concepts'];
  sector_money_flow?: MarketReviewPayload['sector_money_flow'];
  block_trade_heat?: MarketReviewPayload['block_trade_heat'];
};

const isMarketReviewPayload = (value: unknown): value is MarketReviewPayload =>
  Boolean(value && typeof value === 'object');

const TOP_HEADING_PATTERN = /^\s*#\s+(.+?)\s*(?:\n+|$)/;
const SECTION_HEADING_PATTERN = /^(#{2,3})\s+(.+?)\s*$/gm;

const normalizeHeading = (value: string): string =>
  value.trim().replace(/\s+/g, ' ').toLowerCase();

const stripTopHeading = (markdown: string, title?: string): string => {
  const match = markdown.match(TOP_HEADING_PATTERN);
  if (!match) {
    return markdown.trim();
  }

  const heading = normalizeHeading(match[1]);
  const reportTitle = normalizeHeading(title || '');
  const genericTitles = new Set([
    'market review',
    '大盘复盘',
    '大盘复盘详情',
    'a股市场复盘',
    'a 股市场复盘',
  ]);

  if (heading === reportTitle || genericTitles.has(heading)) {
    return markdown.slice(match[0].length).trim();
  }

  return markdown.trim();
};

const getSectionIcon = (title: string): typeof FileText => {
  const normalized = normalizeHeading(title);
  if (/指数|index|overview|大盘/.test(normalized)) {
    return BarChart3;
  }
  if (/情绪|赚钱|sentiment|breadth|temperature/.test(normalized)) {
    return Gauge;
  }
  if (/行业|板块|主题|轮动|sector|theme|rotation/.test(normalized)) {
    return TrendingUp;
  }
  if (/资金|成交|量能|flow|turnover|volume|capital/.test(normalized)) {
    return WalletCards;
  }
  if (/风险|机会|观察|risk|watch|next/.test(normalized)) {
    return ShieldAlert;
  }
  return FileText;
};

const splitMarketReviewSections = (markdown: string): MarketReviewSection[] => {
  const matches = Array.from(markdown.matchAll(SECTION_HEADING_PATTERN));
  if (matches.length === 0) {
    return [{
      id: 'full-review',
      title: '复盘正文',
      content: markdown,
      icon: FileText,
    }];
  }

  const intro = markdown.slice(0, matches[0].index).trim();
  const sections: MarketReviewSection[] = intro
    ? [{
        id: 'overview',
        title: '复盘概览',
        content: intro,
        icon: FileText,
      }]
    : [];

  matches.forEach((match, index) => {
    const start = (match.index ?? 0) + match[0].length;
    const end = index + 1 < matches.length ? matches[index + 1].index ?? markdown.length : markdown.length;
    const title = match[2].trim();
    const content = markdown.slice(start, end).trim();
    if (!content) {
      return;
    }
    sections.push({
      id: `${index}-${normalizeHeading(title).replace(/[^a-z0-9\u4e00-\u9fa5]+/g, '-').replace(/^-|-$/g, '') || 'section'}`,
      title,
      content,
      icon: getSectionIcon(title),
    });
  });

  return sections;
};

const getPayloadSections = (payload?: MarketReviewPayload | null): MarketReviewSection[] => {
  if (!payload) {
    return [];
  }

  if (payload.markets) {
    return Object.entries(payload.markets).flatMap(([region, marketPayload]) => {
      const marketTitle = marketPayload.title || region.toUpperCase();
      return getPayloadSections(marketPayload).map((section) => ({
        ...section,
        id: `${region}-${section.id}`,
        title: `${marketTitle} / ${section.title}`,
      }));
    });
  }

  const payloadTitle = normalizeHeading(payload.title || '');
  return (payload.sections || [])
    .filter((section: MarketReviewPayloadSection) => section.markdown?.trim())
    .filter((section: MarketReviewPayloadSection) => normalizeHeading(section.title || '') !== payloadTitle)
    .map((section, index) => ({
      id: `${section.key || index}-${normalizeHeading(section.title).replace(/[^a-z0-9\u4e00-\u9fa5]+/g, '-') || 'section'}`,
      title: section.title || 'Review',
      content: section.markdown,
      icon: getSectionIcon(section.title || ''),
    }));
};

const hasRankingRows = (rankings?: MarketReviewPayload['sectors']): boolean =>
  Boolean(rankings?.top?.length || rankings?.bottom?.length);

const hasStructuredMarketData = (payload?: MarketReviewPayload | null): boolean =>
  Boolean(
    payload?.breadth ||
      payload?.indices?.length ||
      hasRankingRows(payload?.sectors) ||
      hasRankingRows(payload?.concepts) ||
      (payload?.sector_money_flow?.length ?? 0) > 0,
  );

const getStructuredMarketData = (payload?: MarketReviewPayload | null): StructuredMarketData[] => {
  if (!payload) {
    return [];
  }

  if (payload.markets) {
    return Object.entries(payload.markets)
      .filter(([, marketPayload]) => hasStructuredMarketData(marketPayload))
      .map(([region, marketPayload]) => ({
        id: region,
        title: marketPayload.title || region.toUpperCase(),
        breadth: marketPayload.breadth,
        indices: marketPayload.indices || [],
        sectors: marketPayload.sectors,
        concepts: marketPayload.concepts,
        sector_money_flow: marketPayload.sector_money_flow,
      }));
  }

  if (!hasStructuredMarketData(payload)) {
    return [];
  }

  return [{
    id: payload.region || 'market',
    title: payload.title,
    breadth: payload.breadth,
    indices: payload.indices || [],
    sectors: payload.sectors,
    concepts: payload.concepts,
    sector_money_flow: payload.sector_money_flow,
    block_trade_heat: payload.block_trade_heat,
  }];
};

const coerceFiniteNumber = (value: unknown): number | null => {
  if (typeof value === 'number') {
    return Number.isFinite(value) ? value : null;
  }

  if (typeof value === 'string' && value.trim()) {
    const normalizedValue = value.trim().replace(/,/g, '');
    const numericText = normalizedValue.endsWith('%')
      ? normalizedValue.slice(0, -1).trim()
      : normalizedValue;
    const parsed = Number(numericText);
    return Number.isFinite(parsed) ? parsed : null;
  }

  return null;
};

const formatMarketNumber = (value: unknown, options?: { zeroAsMissing?: boolean }): string => {
  const numericValue = coerceFiniteNumber(value);
  if (numericValue === null || (options?.zeroAsMissing && numericValue === 0)) {
    return '-';
  }
  return numericValue.toFixed(2);
};

const formatMarketCount = (value: unknown): string => {
  const numericValue = coerceFiniteNumber(value);
  return numericValue === null ? '-' : numericValue.toFixed(0);
};

const formatMarketAmount = (value: unknown, unit?: string): string => {
  const formattedValue = formatMarketNumber(value);
  if (formattedValue === '-') {
    return '-';
  }
  return unit ? `${formattedValue} ${unit}` : formattedValue;
};

const formatMarketPercent = (value: unknown): string => {
  const formattedValue = formatMarketNumber(value);
  return formattedValue === '-' ? '-' : `${formattedValue}%`;
};

const formatMarketHighLow = (high: unknown, low: unknown): string => {
  const highText = formatMarketNumber(high, { zeroAsMissing: true });
  const lowText = formatMarketNumber(low, { zeroAsMissing: true });
  return highText === '-' && lowText === '-' ? '-' : `${highText} / ${lowText}`;
};

const MARKET_REVIEW_TEXT: Record<ReportLanguage, {
  reviewSummary: string;
  noReviewSummary: string;
  noSentimentScore: string;
  rotationAndFunds: string;
  noRotationView: string;
  riskAndWatch: string;
  noRiskWatch: string;
  structuredMarketData: string;
  noBreadthData: string;
  advancers: string;
  decliners: string;
  limitUpDown: string;
  turnover: string;
  index: string;
  last: string;
  change: string;
  highLow: string;
  industryBoards: string;
  conceptBoards: string;
  leading: string;
  lagging: string;
  sectorMoneyFlow: string;
  sectorMain: string;
  sectorMain5d: string;
  sectorMid: string;
  sectorRetail: string;
  sectorBlock: string;
  sectorLead: string;
  sectorMoneyFlowHint: string;
  sectorMoneyFlowEmpty: string;
  sectorIntent: string;
  sectorIntentAccumulate: string;
  sectorIntentDistribute: string;
  sectorIntentReflow: string;
  sectorIntentNeutral: string;
  sectorIntentAccumulateDesc: string;
  sectorIntentDistributeDesc: string;
  sectorIntentReflowDesc: string;
}> = {
  zh: {
    reviewSummary: '复盘摘要',
    noReviewSummary: '暂无摘要',
    noSentimentScore: '暂无评分',
    rotationAndFunds: '轮动与资金',
    noRotationView: '暂无轮动观点',
    riskAndWatch: '风险与观察',
    noRiskWatch: '暂无观察重点',
    structuredMarketData: '结构化大盘数据',
    noBreadthData: '暂无数据',
    advancers: '上涨家数',
    decliners: '下跌家数',
    limitUpDown: '涨停/跌停',
    turnover: '成交额',
    index: '指数',
    last: '最新',
    change: '涨跌幅',
    highLow: '高/低',
    industryBoards: '行业板块',
    conceptBoards: '概念板块',
    leading: '领涨',
    lagging: '领跌',
    sectorMoneyFlow: '板块资金流（主力 / 中户 / 散户 / 暗盘）',
    sectorMain: '主力',
    sectorMain5d: '5日主力',
    sectorMid: '中户',
    sectorRetail: '散户',
    sectorBlock: '暗盘',
    sectorLead: '领涨股',
    sectorMoneyFlowHint:
      '阅读提示：主力净流入显著为正、散户净流入为负时，说明大资金在低位吸收散户筹码；股价可能滞涨或回调，但实际是主力吸筹。暗盘（大宗交易）若与主力同向，可视为机构调仓的额外信号。',
    sectorMoneyFlowEmpty: '板块资金流数据暂不可用',
    sectorIntent: '主力意图',
    sectorIntentAccumulate: '主力吸筹',
    sectorIntentDistribute: '主力出货',
    sectorIntentReflow: '主力回流',
    sectorIntentNeutral: '观望',
    sectorIntentAccumulateDesc: '主力流入 + 散户流出，经典吸筹信号',
    sectorIntentDistributeDesc: '主力流出，无暗盘托盘',
    sectorIntentReflowDesc: '主力流出，但暗盘正向接盘（机构反向接货）',
  },
  en: {
    reviewSummary: 'Review Summary',
    noReviewSummary: 'No review summary yet',
    noSentimentScore: 'No score yet',
    rotationAndFunds: 'Rotation & Funds',
    noRotationView: 'No rotation view yet',
    riskAndWatch: 'Risks & Watchlist',
    noRiskWatch: 'No key observations yet',
    structuredMarketData: 'Structured Market Data',
    noBreadthData: 'No data',
    advancers: 'Advancers',
    decliners: 'Decliners',
    limitUpDown: 'Limit Up/Down',
    turnover: 'Turnover',
    index: 'Index',
    last: 'Last',
    change: 'Change',
    highLow: 'High/Low',
    industryBoards: 'Industry Sectors',
    conceptBoards: 'Concept Themes',
    leading: 'Leading',
    lagging: 'Lagging',
    sectorMoneyFlow: 'Sector Money Flow (Main / Mid / Retail / Dark Pool)',
    sectorMain: 'Main',
    sectorMain5d: '5D Main',
    sectorMid: 'Mid',
    sectorRetail: 'Retail',
    sectorBlock: 'Dark Pool',
    sectorLead: 'Lead Stock',
    sectorMoneyFlowHint:
      'Reading hint: when Main is strongly positive while Retail is negative, large funds are absorbing retail chips; price may stagnate, but this is classic accumulation. Block trades moving with Main reinforce the institutional positioning thesis.',
    sectorMoneyFlowEmpty: 'Sector money flow data is currently unavailable',
    sectorIntent: 'Main Intent',
    sectorIntentAccumulate: 'Accumulation',
    sectorIntentDistribute: 'Distribution',
    sectorIntentReflow: 'Reflow',
    sectorIntentNeutral: 'Neutral',
    sectorIntentAccumulateDesc: 'Main inflow + retail outflow = classic accumulation',
    sectorIntentDistributeDesc: 'Main outflow with no dark-pool support',
    sectorIntentReflowDesc: 'Main outflow but dark pool absorbing (institutional re-entry)',
  },
  ko: {
    reviewSummary: '리뷰 요약',
    noReviewSummary: '요약 없음',
    noSentimentScore: '점수 없음',
    rotationAndFunds: '순환과 자금',
    noRotationView: '순환 관점 없음',
    riskAndWatch: '리스크와 관찰',
    noRiskWatch: '관찰 포인트 없음',
    structuredMarketData: '구조화 시장 데이터',
    noBreadthData: '데이터 없음',
    advancers: '상승 종목 수',
    decliners: '하락 종목 수',
    limitUpDown: '상한가/하한가',
    turnover: '거래대금',
    index: '지수',
    last: '현재',
    change: '등락률',
    highLow: '고가/저가',
    industryBoards: '업종 섹터',
    conceptBoards: '테마 섹터',
    leading: '강세',
    lagging: '약세',
    sectorMoneyFlow: '섹터 자금 흐름 (주력 / 중간 / 개인 / 다크풀)',
    sectorMain: '주력',
    sectorMain5d: '5일 주력',
    sectorMid: '중간',
    sectorRetail: '개인',
    sectorBlock: '다크풀',
    sectorLead: '선두 종목',
    sectorMoneyFlowHint:
      '주력 자금 유입이 강한 양수이고 개인 자금 유입이 음수일 때, 대형 기관이 저점에서 개인 물량을 흡수하고 있음을 의미합니다. 가격이 정체되거나 하락해도 실제로는 주력 매집 단계입니다.',
    sectorMoneyFlowEmpty: '섹터 자금 흐름 데이터를 사용할 수 없습니다',
    sectorIntent: '주력 의도',
    sectorIntentAccumulate: '매집',
    sectorIntentDistribute: '분배',
    sectorIntentReflow: '재유입',
    sectorIntentNeutral: '관망',
    sectorIntentAccumulateDesc: '주력 유입 + 개인 유출 = 전형적인 매집',
    sectorIntentDistributeDesc: '주력 유출, 다크풀 지지 없음',
    sectorIntentReflowDesc: '주력 유출이지만 다크풀 흡수 (기관 재진입)',
  },
};

const formatRankingChange = (value: unknown): string => {
  const numeric = typeof value === 'number' ? value : Number(String(value ?? '').replace(/%$/, ''));
  if (!Number.isFinite(numeric)) {
    return '-';
  }
  const sign = numeric > 0 ? '+' : '';
  return `${sign}${numeric.toFixed(2)}%`;
};

const formatYiMoney = (yuanValue: unknown): string => {
  const numeric = coerceFiniteNumber(yuanValue);
  if (numeric === null) {
    return '-';
  }
  const yi = numeric / 1e8;
  const sign = yi > 0 ? '+' : yi < 0 ? '' : '';
  return `${sign}${yi.toFixed(2)}亿`;
};

const toneForMoney = (value: unknown): 'up' | 'down' | 'neutral' => {
  const numeric = coerceFiniteNumber(value);
  if (numeric === null || numeric === 0) return 'neutral';
  return numeric > 0 ? 'up' : 'down';
};

type MoneyText = {
  sectorMoneyFlow: string;
  sectorMain: string;
  sectorMain5d: string;
  sectorMid: string;
  sectorRetail: string;
  sectorBlock: string;
  sectorLead: string;
  sectorMoneyFlowHint: string;
  sectorMoneyFlowEmpty: string;
  sectorIntent: string;
  sectorIntentAccumulate: string;
  sectorIntentDistribute: string;
  sectorIntentReflow: string;
  sectorIntentNeutral: string;
  sectorIntentAccumulateDesc: string;
  sectorIntentDistributeDesc: string;
  sectorIntentReflowDesc: string;
};

const renderSectorMoneyFlowSection = (
  rows: NonNullable<MarketReviewPayload['sector_money_flow']>,
  text: MoneyText,
  blockTradeHeat?: MarketReviewPayload['block_trade_heat'],
) => {
  if (!rows.length) return null;
  return (
    <div className="rounded-lg border border-subtle p-3">
      <div className="mb-3 flex items-center justify-between gap-2">
        <p className="label-uppercase">{text.sectorMoneyFlow}</p>
        <span className="text-xs text-secondary-text">{rows.length}</span>
      </div>
      <div className="overflow-x-auto">
        <table className="min-w-full text-sm">
          <thead className="text-left text-xs uppercase text-muted-text">
            <tr>
              <th className="px-2 py-2">板块</th>
              <th className="px-2 py-2">{text.sectorLead}</th>
              <th className="px-2 py-2 text-right">{text.sectorMain}</th>
              <th className="px-2 py-2 text-right">{text.sectorMain5d}</th>
              <th className="px-2 py-2 text-right">{text.sectorMid}</th>
              <th className="px-2 py-2 text-right">{text.sectorRetail}</th>
              <th className="px-2 py-2 text-right">{text.sectorBlock}</th>
              <th className="px-2 py-2 text-center">{text.sectorIntent}</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-subtle">
            {rows.slice(0, 10).map((row, index) => (
              <tr key={`${row.name}-${index}`}>
                <td className="px-2 py-2 font-medium text-foreground">
                  <div className="flex flex-col">
                    <span>{row.name || '-'}</span>
                    <span className="text-xs text-secondary-text">
                      {formatRankingChange(row.pct_change)}
                    </span>
                  </div>
                </td>
                <td className="px-2 py-2 text-secondary-text">{row.lead_stock || '-'}</td>
                <MoneyCell value={row.main_net} tone={toneForMoney(row.main_net)} />
                <MoneyCell value={row.main_net_5d} tone={toneForMoney(row.main_net_5d)} />
                <MoneyCell value={row.mid_net} tone={toneForMoney(row.mid_net)} />
                <MoneyCell value={row.retail_net} tone={toneForMoney(row.retail_net)} />
                <MoneyCell value={row.block_net} tone={toneForMoney(row.block_net)} />
                <IntentCell intent={row.intent} text={text} />
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mt-3 text-xs leading-relaxed text-secondary-text">{text.sectorMoneyFlowHint}</p>
      {blockTradeHeat && blockTradeHeat.length > 0
        ? renderBlockTradeHeatSection(blockTradeHeat, text)
        : null}
    </div>
  );
};

const IntentCell: React.FC<{ intent?: string; text: MoneyText }> = ({ intent, text }) => {
  const meta = (() => {
    switch (intent) {
      case 'accumulate':
        return { label: text.sectorIntentAccumulate, className: 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-300', desc: text.sectorIntentAccumulateDesc };
      case 'distribute':
        return { label: text.sectorIntentDistribute, className: 'bg-red-500/15 text-red-600 dark:text-red-300', desc: text.sectorIntentDistributeDesc };
      case 'reflow':
        return { label: text.sectorIntentReflow, className: 'bg-amber-500/15 text-amber-600 dark:text-amber-300', desc: text.sectorIntentReflowDesc };
      case 'neutral':
      default:
        return { label: text.sectorIntentNeutral, className: 'bg-secondary/30 text-secondary-text', desc: '' };
    }
  })();
  return (
    <td className="px-2 py-2 text-center">
      <span
        className={`inline-block max-w-full rounded px-1.5 py-0.5 text-xs font-medium ${meta.className}`}
        title={meta.desc}
      >
        {meta.label}
      </span>
    </td>
  );
};

const renderBlockTradeHeatSection = (
  trades: NonNullable<MarketReviewPayload['block_trade_heat']>,
  text: MoneyText,
) => {
  if (!trades.length) return null;
  return (
    <div className="mt-3 rounded border border-secondary px-3 py-2">
      <p className="mb-2 text-xs font-semibold text-accent">
        {text.sectorBlock}热点（暗盘最活跃的行业）
      </p>
      <div className="space-y-1.5">
        {trades.map((t, i) => (
          <div key={i} className="flex items-center justify-between text-xs">
            <span className="font-medium min-w-[80px]">{t.name}</span>
            <span className="text-muted-text">{formatRankingChange(t.pct_change)}</span>
            <span className="text-[#34d399] dark:text-[#6ee7b7]">{formatYiMoney(t.block_net)}</span>
            <span className="text-secondary-text truncate ml-2">{t.lead_stock || ''}</span>
          </div>
        ))}
      </div>
    </div>
  );
};

const MoneyCell: React.FC<{ value: unknown; tone: 'up' | 'down' | 'neutral' }> = ({ value, tone }) => {
  const toneClass =
    tone === 'up'
      ? 'text-[#f87171] dark:text-[#fca5a5]'
      : tone === 'down'
        ? 'text-[#34d399] dark:text-[#6ee7b7]'
        : 'text-secondary-text';
  return (
    <td className={`px-2 py-2 text-right font-mono tabular-nums ${toneClass}`}>
      {formatYiMoney(value)}
    </td>
  );
};

export const MarketReviewReportView: React.FC<MarketReviewReportViewProps> = ({
  report,
  recordId,
  content: providedContent,
  payload: providedPayload,
  reportLanguage = 'zh',
  className = '',
  onOpenRunFlow,
}) => {
  const normalizedReportLanguage = normalizeReportLanguage(reportLanguage);
  const text = getReportText(normalizedReportLanguage);
  const runFlowText = UI_TEXT[normalizedReportLanguage === 'ko' ? 'en' : normalizedReportLanguage];
  const marketReviewText = MARKET_REVIEW_TEXT[normalizedReportLanguage];
  const [loadedMarkdown, setLoadedMarkdown] = useState<LoadedMarkdown | null>(null);
  const [loadError, setLoadError] = useState<LoadError | null>(null);
  const [copiedType, setCopiedType] = useState<CopyType | null>(null);
  const summary = report?.summary;
  const meta = report?.meta;
  const contextPayload = report?.details?.contextSnapshot?.marketReviewPayload;
  const marketReviewPayload = providedPayload ?? (isMarketReviewPayload(contextPayload) ? contextPayload : null);
  const loadedContent = loadedMarkdown && loadedMarkdown.recordId === recordId ? loadedMarkdown.content : '';
  const content = providedContent ?? marketReviewPayload?.markdownReport ?? loadedContent;
  const error = loadError && loadError.recordId === recordId ? loadError.message : null;
  const hasStructuredContent = Boolean(marketReviewPayload?.sections?.length || marketReviewPayload?.markets);
  const isLoading = Boolean(recordId && !providedContent && !hasStructuredContent && loadedMarkdown?.recordId !== recordId && !error);
  const displayTitle = marketReviewPayload?.rootTitle || marketReviewPayload?.title || meta?.stockName || 'Market Review';
  const structuredContent = useMemo(
    () => stripTopHeading(content, displayTitle),
    [content, displayTitle],
  );
  const sections = useMemo(
    () => {
      const payloadSections = getPayloadSections(marketReviewPayload);
      return payloadSections.length > 0 ? payloadSections : splitMarketReviewSections(structuredContent);
    },
    [marketReviewPayload, structuredContent],
  );
  const structuredMarketData = useMemo(
    () => getStructuredMarketData(marketReviewPayload),
    [marketReviewPayload],
  );
  const showStructuredMarketTitles = Boolean(marketReviewPayload?.markets);
  const canOpenRunFlow = recordId !== undefined && onOpenRunFlow;

  useEffect(() => {
    if (!recordId || providedContent || hasStructuredContent) {
      return undefined;
    }

    let isMounted = true;

    historyApi.getMarkdown(recordId)
      .then((markdownContent) => {
        if (isMounted) {
          setLoadedMarkdown({ recordId, content: markdownContent });
          setLoadError(null);
        }
      })
      .catch((err: unknown) => {
        if (isMounted) {
          setLoadError({
            recordId,
            message: err instanceof Error ? err.message : text.loadReportFailed,
          });
        }
      });

    return () => {
      isMounted = false;
    };
  }, [hasStructuredContent, providedContent, recordId, text.loadReportFailed]);

  const handleCopy = useCallback(async (type: CopyType) => {
    if (!content) {
      return;
    }
    try {
      const value = type === 'markdown' ? content : markdownToPlainText(content);
      await navigator.clipboard.writeText(value);
      setCopiedType(type);
      window.setTimeout(() => setCopiedType(null), 2000);
    } catch (err) {
      console.error('Copy failed:', err);
    }
  }, [content]);

  const insightCards = useMemo(() => [
    {
      icon: FileText,
      label: marketReviewText.reviewSummary,
      value: summary?.analysisSummary || marketReviewText.noReviewSummary,
    },
    {
      icon: Gauge,
      label: text.marketSentiment,
      value: summary?.sentimentScore !== undefined
        ? `${summary.sentimentScore} / 100`
        : marketReviewText.noSentimentScore,
    },
    {
      icon: Layers,
      label: marketReviewText.rotationAndFunds,
      value: summary?.operationAdvice || marketReviewText.noRotationView,
    },
    {
      icon: ShieldAlert,
      label: marketReviewText.riskAndWatch,
      value: summary?.trendPrediction || marketReviewText.noRiskWatch,
    },
  ], [marketReviewText, summary, text.marketSentiment]);

  return (
    <div className={`animate-fade-in space-y-4 pb-8 ${className}`}>
      <Card variant="gradient" padding="md" className="home-report-hero text-left">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="mb-2 inline-flex items-center gap-2 text-xs font-semibold text-secondary-text">
              <BarChart3 className="h-4 w-4" aria-hidden="true" />
              <span>MARKET REVIEW</span>
            </div>
            <h2 className="text-[26px] font-bold leading-tight text-foreground sm:text-[30px]">
              {displayTitle}
            </h2>
            <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-text">
              {meta?.stockCode ? (
                <span className="home-accent-chip px-2 py-0.5 font-mono">{meta.stockCode}</span>
              ) : null}
              {meta?.createdAt ? <span>{new Date(meta.createdAt).toLocaleString()}</span> : null}
            </div>
          </div>

          <div className="flex shrink-0 items-center gap-2">
            <ShareImageButton
              recordId={recordId}
              reportTitle={displayTitle}
              reportLanguage={reportLanguage}
            />
            {canOpenRunFlow ? (
              <Tooltip content={runFlowText['runFlow.open']}>
                <span className="inline-flex">
                  <button
                    type="button"
                    onClick={() => onOpenRunFlow(recordId)}
                    className="home-surface-button flex h-10 w-10 items-center justify-center rounded-lg text-secondary-text hover:text-foreground"
                    aria-label={formatUiText(runFlowText['runFlow.openHistoryAria'], { recordId })}
                  >
                    <Workflow className="h-5 w-5" aria-hidden="true" />
                  </button>
                </span>
              </Tooltip>
            ) : null}
            <Tooltip content={text.copyMarkdownSource}>
              <span className="inline-flex">
                <button
                  type="button"
                  onClick={() => void handleCopy('markdown')}
                  disabled={isLoading || !content || copiedType !== null}
                  className="home-surface-button flex h-10 w-10 items-center justify-center rounded-lg text-secondary-text hover:text-foreground disabled:opacity-50"
                  aria-label={text.copyMarkdownSource}
                >
                  {copiedType === 'markdown' ? (
                    <svg className="h-5 w-5 text-success" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                    </svg>
                  ) : (
                    <Clipboard className="h-5 w-5" aria-hidden="true" />
                  )}
                </button>
              </span>
            </Tooltip>
            <Tooltip content={text.copyPlainText}>
              <span className="inline-flex">
                <button
                  type="button"
                  onClick={() => void handleCopy('text')}
                  disabled={isLoading || !content || copiedType !== null}
                  className="home-surface-button flex h-10 w-10 items-center justify-center rounded-lg text-secondary-text hover:text-foreground disabled:opacity-50"
                  aria-label={text.copyPlainText}
                >
                  {copiedType === 'text' ? (
                    <svg className="h-5 w-5 text-success" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                    </svg>
                  ) : (
                    <FileText className="h-5 w-5" aria-hidden="true" />
                  )}
                </button>
              </span>
            </Tooltip>
          </div>
        </div>
      </Card>

      {summary ? (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
          {insightCards.map(({ icon: Icon, label, value }) => (
            <Card key={label} variant="bordered" padding="sm" className="home-panel-card text-left">
              <div className="flex items-start gap-3">
                <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
                  <Icon className="h-4 w-4" aria-hidden="true" />
                </div>
                <div className="min-w-0">
                  <p className="label-uppercase">{label}</p>
                  <p className="mt-2 line-clamp-4 text-sm leading-6 text-foreground">{value}</p>
                </div>
              </div>
            </Card>
          ))}
        </div>
      ) : null}

      {structuredMarketData.length > 0 ? (
        <Card variant="bordered" padding="md" className="home-panel-card text-left">
          <div className="mb-3 flex items-center gap-2">
            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <BarChart3 className="h-4 w-4" aria-hidden="true" />
            </span>
            <h3 className="text-base font-semibold text-foreground">{marketReviewText.structuredMarketData}</h3>
          </div>
          <div className="space-y-5">
            {structuredMarketData.map((marketData) => (
              <div key={marketData.id} className="space-y-3">
                {showStructuredMarketTitles ? (
                  <h4 className="text-sm font-semibold text-foreground">{marketData.title}</h4>
                ) : null}
                {marketData.breadth ? (
                  <div className="grid grid-cols-2 gap-2 text-sm md:grid-cols-4">
                    <div className="rounded-lg border border-subtle p-3">
                      <p className="label-uppercase">{marketReviewText.advancers}</p>
                      <p className="mt-1 font-semibold text-foreground">
                        {formatMarketCount(marketData.breadth.upCount)}
                      </p>
                    </div>
                    <div className="rounded-lg border border-subtle p-3">
                      <p className="label-uppercase">{marketReviewText.decliners}</p>
                      <p className="mt-1 font-semibold text-foreground">
                        {formatMarketCount(marketData.breadth.downCount)}
                      </p>
                    </div>
                    <div className="rounded-lg border border-subtle p-3">
                      <p className="label-uppercase">{marketReviewText.limitUpDown}</p>
                      <p className="mt-1 font-semibold text-foreground">
                        {formatMarketCount(marketData.breadth.limitUpCount)} /{' '}
                        {formatMarketCount(marketData.breadth.limitDownCount)}
                      </p>
                    </div>
                    <div className="rounded-lg border border-subtle p-3">
                      <p className="label-uppercase">{marketReviewText.turnover}</p>
                      <p className="mt-1 font-semibold text-foreground">
                        {formatMarketAmount(marketData.breadth.totalAmount, marketData.breadth.turnoverUnit)}
                      </p>
                    </div>
                  </div>
                ) : (
                  <p className="text-sm text-secondary-text">{marketReviewText.noBreadthData}</p>
                )}
                {marketData.indices.length > 0 ? (
                  <div className="overflow-x-auto">
                    <table className="min-w-full text-sm">
                      <thead className="text-left text-xs uppercase text-muted-text">
                        <tr>
                          <th className="px-2 py-2">{marketReviewText.index}</th>
                          <th className="px-2 py-2">{marketReviewText.last}</th>
                          <th className="px-2 py-2">{marketReviewText.change}</th>
                          <th className="px-2 py-2">{marketReviewText.highLow}</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-subtle">
                        {marketData.indices.map((index) => (
                          <tr key={index.code || index.name}>
                            <td className="px-2 py-2 font-medium text-foreground">{index.name}</td>
                            <td className="px-2 py-2 text-secondary-text">{formatMarketNumber(index.current)}</td>
                            <td className="px-2 py-2 text-secondary-text">{formatMarketPercent(index.changePct)}</td>
                            <td className="px-2 py-2 text-secondary-text">{formatMarketHighLow(index.high, index.low)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : null}
                {(() => {
                  const boardTypes = [{
                    key: 'sectors' as const,
                    title: marketReviewText.industryBoards,
                    rankings: marketData.sectors,
                  }, {
                    key: 'concepts' as const,
                    title: marketReviewText.conceptBoards,
                    rankings: marketData.concepts,
                  }].filter(({ rankings }) => hasRankingRows(rankings));
                  if (boardTypes.length === 0) {
                    return null;
                  }
                  const renderPanels = (
                    key: string,
                    title: string,
                    rankings: MarketReviewPayload['sectors'],
                  ) => (['top', 'bottom'] as const).map((side) => {
                    const rows = rankings?.[side] || [];
                    if (rows.length === 0) {
                      return null;
                    }
                    return (
                      <div key={`${key}-${side}`} className="rounded-lg border border-subtle p-3">
                        <div className="mb-2 flex items-center justify-between gap-2">
                          <p className="label-uppercase">{title}</p>
                          <span className="text-xs text-secondary-text">
                            {side === 'top' ? marketReviewText.leading : marketReviewText.lagging}
                          </span>
                        </div>
                        <div className="space-y-1.5">
                          {rows.slice(0, 5).map((item, index) => (
                            <div key={`${item.name}-${index}`} className="flex items-center justify-between gap-3 text-sm">
                              <span className="min-w-0 truncate text-foreground">{item.name}</span>
                              <span className="shrink-0 font-mono text-secondary-text">
                                {formatRankingChange(item.changePct)}
                              </span>
                            </div>
                          ))}
                        </div>
                      </div>
                    );
                  });
                  // 两类板块都存在时按 行业|概念 左右并列，节省纵向空间；只有一类时保留 领涨|领跌 横向布局。
                  if (boardTypes.length >= 2) {
                    return (
                      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                        {boardTypes.map(({ key, title, rankings }) => (
                          <div key={key} className="space-y-3">
                            {renderPanels(key, title, rankings)}
                          </div>
                        ))}
                      </div>
                    );
                  }
                  const { key, title, rankings } = boardTypes[0];
                  return (
                    <div key={key} className="grid grid-cols-1 gap-3 md:grid-cols-2">
                      {renderPanels(key, title, rankings)}
                    </div>
                  );
                })()}
                {marketData.sector_money_flow && marketData.sector_money_flow.length > 0
                  ? renderSectorMoneyFlowSection(marketData.sector_money_flow, marketReviewText, marketData.block_trade_heat)
                  : null}
              </div>
            ))}
          </div>
        </Card>
      ) : null}

      {isLoading ? (
        <Card variant="bordered" padding="md" className="home-panel-card text-left">
          <div className="flex h-64 flex-col items-center justify-center">
            <div className="home-spinner h-10 w-10 animate-spin border-[3px]" />
            <p className="mt-4 text-sm text-secondary-text">{text.loadingReport}</p>
          </div>
        </Card>
      ) : error ? (
        <Card variant="bordered" padding="md" className="home-panel-card text-left">
          <div className="flex h-64 flex-col items-center justify-center">
            <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-danger/10">
              <ShieldAlert className="h-6 w-6 text-danger" aria-hidden="true" />
            </div>
            <p className="text-sm text-danger">{error}</p>
          </div>
        </Card>
      ) : (
        <div data-testid="market-review-report" className="space-y-4">
          {sections.map(({ id, title, content: sectionContent, icon: Icon }) => (
            <Card key={id} variant="bordered" padding="md" className="home-panel-card text-left">
              <div className="mb-3 flex items-center gap-2">
                <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
                  <Icon className="h-4 w-4" aria-hidden="true" />
                </span>
                <h3 className="text-base font-semibold text-foreground">{title}</h3>
              </div>
              <ReportMarkdownBody
                content={sectionContent}
                className="market-review-markdown"
              />
            </Card>
          ))}
        </div>
      )}
    </div>
  );
};
