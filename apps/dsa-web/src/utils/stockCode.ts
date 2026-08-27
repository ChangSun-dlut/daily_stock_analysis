/**
 * Normalize stock code by stripping exchange prefixes/suffixes.
 *
 * Mirrors the behavior of data_provider.base.normalize_stock_code in the backend.
 *
 *   600519      → 600519     SH600519    → 600519
 *   600519.SH   → 600519     SH.600519   → 600519
 *   SZ000001    → 000001     000001.SZ   → 000001
 *   BJ920748    → 920748     920748.BJ   → 920748
 *   HK00700     → HK00700    00700       → HK00700
 *   00700.HK    → HK00700
 *   hk1810      → HK01810    1810.HK     → HK01810
 *   7203.T      → 7203.T     005930.KS   → 005930.KS
 *   AAPL        → AAPL       TSLA        → TSLA
 */
export function normalizeStockCode(stockCode: string): string {
  const code = stockCode.trim();
  const upper = code.toUpperCase();

  // Normalize HK prefix to a canonical 5-digit form (e.g. hk1810 → HK01810)
  if (upper.startsWith('HK') && !upper.startsWith('HK.')) {
    const candidate = upper.slice(2);
    if (/^\d{1,5}$/.test(candidate) && candidate.length >= 1 && candidate.length <= 5) {
      return `HK${candidate.padStart(5, '0')}`;
    }
  }

  // Pure 5-digit codes are HK stocks by validateStockCode() contract.
  if (/^\d{5}$/.test(upper)) {
    return `HK${upper}`;
  }

  // Strip SH/SZ prefix (e.g. SH600519 → 600519)
  if ((upper.startsWith('SH') || upper.startsWith('SZ')) && !upper.startsWith('SH.') && !upper.startsWith('SZ.')) {
    const candidate = code.slice(2);
    if (/^\d{5,6}$/.test(candidate)) {
      return candidate;
    }
  }

  // Strip dotted SH/SZ prefix (e.g. SH.600519 → 600519)
  if (upper.startsWith('SH.') || upper.startsWith('SZ.')) {
    const candidate = code.slice(3);
    if (/^\d{5,6}$/.test(candidate)) {
      return candidate;
    }
  }

  // Strip BJ prefix (e.g. BJ920748 → 920748)
  if (upper.startsWith('BJ') && !upper.startsWith('BJ.')) {
    const candidate = code.slice(2);
    if (/^\d{6}$/.test(candidate)) {
      return candidate;
    }
  }

  // Strip dotted BJ prefix (e.g. BJ.920748 → 920748)
  if (upper.startsWith('BJ.')) {
    const candidate = code.slice(3);
    if (/^\d{6}$/.test(candidate)) {
      return candidate;
    }
  }

  // Strip .SH/.SZ/.BJ suffix and .HK suffix with HK-prefix canonicalization
  if (code.includes('.')) {
    const dotIndex = code.lastIndexOf('.');
    const base = code.slice(0, dotIndex);
    const suffix = code.slice(dotIndex + 1).toUpperCase();

    // JP/KR Yahoo suffix-only codes are canonical as uppercase suffix forms.
    if (suffix === 'T' && /^\d{4,5}$/.test(base)) {
      return `${base}.${suffix}`;
    }
    if ((suffix === 'KS' || suffix === 'KQ') && /^\d{6}$/.test(base)) {
      return `${base}.${suffix}`;
    }
    // TW Yahoo suffix-only codes (TWSE `.TW` / TPEx `.TWO`), base 4-6 digits.
    if ((suffix === 'TW' || suffix === 'TWO') && /^\d{4,6}$/.test(base)) {
      return `${base}.${suffix}`;
    }

    // 00700.HK → HK00700
    if (suffix === 'HK' && /^\d{1,5}$/.test(base)) {
      return `HK${base.padStart(5, '0')}`;
    }

    // 600519.SH → 600519
    if ((suffix === 'SH' || suffix === 'SS' || suffix === 'SZ' || suffix === 'BJ') && /^\d+$/.test(base)) {
      return base;
    }
  }

  return code;
}

/**
 * 根据股票代码推断所属市场，用于前端在无法拿到 market 字段时判断使用哪个
 * 市场阶段/交易时段规则（如 A 股 watchlist 实时行情卡片）。
 */
export function getMarketFromStockCode(stockCode: string): string {
  const code = stockCode.trim();
  const upper = code.toUpperCase();

  if (/^\d{6}\.(SH|SZ|BJ|SS)$/.test(code)) return 'cn';
  if (/^(SH|SZ|BJ)\d{6}$/.test(upper)) return 'cn';
  if (/^(SH|SZ|BJ)\.\d{6}$/.test(upper)) return 'cn';

  if (/^\d{5}\.HK$/.test(code) || /^HK\d{5}$/.test(upper) || /^HK\.\d{5}$/.test(upper)) {
    return 'hk';
  }

  if (/^\d{4,5}\.T$/.test(code)) return 'jp';
  if (/^\d{6}\.(KS|KQ)$/.test(code)) return 'kr';
  if (/^\d{4,6}\.(TW|TWO)$/.test(code)) return 'tw';
  if (/\.(US|NYSE|NASDAQ|AMEX)$/.test(code) || /^[A-Z]{1,5}$/.test(upper)) return 'us';

  return 'other';
}

function stockCodeMatchKey(stockCode: string): string {
  return normalizeStockCode(stockCode).toUpperCase();
}

export function areStockCodesEquivalent(left: string, right: string): boolean {
  if (!left.trim() || !right.trim()) return false;
  return stockCodeMatchKey(left) === stockCodeMatchKey(right);
}

export function findMatchingStockCode(codes: string[], stockCode: string): string | undefined {
  if (!stockCode.trim()) return undefined;
  const targetKey = stockCodeMatchKey(stockCode);
  return codes.find((code) => code.trim() && stockCodeMatchKey(code) === targetKey);
}

export function includesStockCode(codes: string[], stockCode: string): boolean {
  return findMatchingStockCode(codes, stockCode) !== undefined;
}
