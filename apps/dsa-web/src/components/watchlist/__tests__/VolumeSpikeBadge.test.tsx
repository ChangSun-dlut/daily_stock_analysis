import type React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { VolumeSpikeBadge } from '../VolumeSpikeBadge';
import { UiLanguageProvider } from '../../../contexts/UiLanguageContext';

function renderWithLang(ui: React.ReactElement) {
  return render(<UiLanguageProvider>{ui}</UiLanguageProvider>);
}

beforeEach(() => {
  window.localStorage.setItem('dsa.uiLanguage', 'zh');
});

afterEach(() => {
  vi.useRealTimers();
});

describe('VolumeSpikeBadge', () => {
  it('renders unavailable label by default when ratio is null', () => {
    renderWithLang(<VolumeSpikeBadge ratio={null} changePercent={null} />);
    expect(screen.getByText('量比暂无')).toBeInTheDocument();
  });

  it('renders phase-specific label for A-share premarket', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-08-25T07:51:00+08:00'));
    renderWithLang(<VolumeSpikeBadge ratio={null} changePercent={null} market="cn" />);
    expect(screen.getByText('未开盘')).toBeInTheDocument();
  });

  it('renders normal when ratio is below spike threshold', () => {
    renderWithLang(<VolumeSpikeBadge ratio={1.5} changePercent={null} />);
    expect(screen.getByText('量正常')).toBeInTheDocument();
  });

  it('renders spike label when ratio >= 2', () => {
    renderWithLang(<VolumeSpikeBadge ratio={2.5} changePercent={1.2} />);
    expect(screen.getByText('放量 2.50x')).toBeInTheDocument();
  });

  it('renders surge label when ratio >= 5', () => {
    renderWithLang(<VolumeSpikeBadge ratio={5.5} changePercent={-0.5} />);
    expect(screen.getByText('巨量 5.50x')).toBeInTheDocument();
  });
});
