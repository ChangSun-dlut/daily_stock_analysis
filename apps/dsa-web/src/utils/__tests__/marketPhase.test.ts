import { describe, expect, it } from 'vitest';

import { getCurrentAshareMarketPhase, getMarketPhaseSummaryLabel } from '../marketPhase';

describe('getCurrentAshareMarketPhase', () => {
  it('returns premarket before 09:30 on weekdays', () => {
    expect(getCurrentAshareMarketPhase(new Date('2026-08-25T07:51:00+08:00'))).toBe('premarket');
  });

  it('returns intraday in the morning session', () => {
    expect(getCurrentAshareMarketPhase(new Date('2026-08-25T10:00:00+08:00'))).toBe('intraday');
  });

  it('returns lunch_break between 11:30 and 13:00', () => {
    expect(getCurrentAshareMarketPhase(new Date('2026-08-25T12:00:00+08:00'))).toBe('lunch_break');
  });

  it('returns intraday in the afternoon session', () => {
    expect(getCurrentAshareMarketPhase(new Date('2026-08-25T14:00:00+08:00'))).toBe('intraday');
  });

  it('returns postmarket after 15:00 on weekdays', () => {
    expect(getCurrentAshareMarketPhase(new Date('2026-08-25T18:00:00+08:00'))).toBe('postmarket');
  });

  it('returns non_trading on weekends', () => {
    expect(getCurrentAshareMarketPhase(new Date('2026-08-23T10:00:00+08:00'))).toBe('non_trading');
  });

  it('handles UTC input by converting to Asia/Shanghai', () => {
    // 2026-08-24 23:30 UTC = 2026-08-25 07:30 Asia/Shanghai, premarket.
    expect(getCurrentAshareMarketPhase(new Date('2026-08-24T23:30:00Z'))).toBe('premarket');
  });
});

describe('getMarketPhaseSummaryLabel', () => {
  it('renders market and phase label', () => {
    expect(getMarketPhaseSummaryLabel({ phase: 'intraday', market: 'cn', warnings: [] }, 'zh')).toContain('盘中');
    expect(getMarketPhaseSummaryLabel({ phase: 'lunch_break', market: 'cn', warnings: [] }, 'zh')).toContain('午间休市');
  });
});
