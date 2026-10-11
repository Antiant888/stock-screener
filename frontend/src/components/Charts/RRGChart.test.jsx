import { describe, it, expect } from 'vitest';
import { screen, fireEvent, renderHook } from '@testing-library/react';
import { ThemeProvider, createTheme } from '@mui/material';
import { renderWithProviders } from '../../test/renderWithProviders';
import RRGChart from './RRGChart';
import { QUADRANT_TONES, useQuadrantColors } from './rrgColors';
import { getDesignTokens } from '../../theme';

const sampleData = {
  date: '2024-09-29',
  market: 'US',
  scope: 'groups',
  groups: [
    {
      industry_group: 'AlphaTech',
      rank: 1,
      num_stocks: 12,
      avg_rs_rating: 64.0,
      quadrant: 'Leading',
      is_provisional: false,
      current: { date: '2024-09-29', x: 108.3, y: 106.1 },
      tail: [
        { date: '2024-08-11', x: 104.0, y: 98.0 },
        { date: '2024-09-29', x: 108.3, y: 106.1 },
      ],
    },
    {
      industry_group: 'BetaMetals',
      rank: 2,
      num_stocks: 8,
      avg_rs_rating: 36.0,
      quadrant: 'Lagging',
      is_provisional: false,
      current: { date: '2024-09-29', x: 91.7, y: 93.9 },
      tail: [
        { date: '2024-08-11', x: 95.0, y: 99.0 },
        { date: '2024-09-29', x: 91.7, y: 93.9 },
      ],
    },
  ],
};

describe('RRGChart', () => {
  it('renders the header with scope and as-of date', () => {
    renderWithProviders(<RRGChart data={sampleData} />);
    expect(screen.getByText(/Relative Rotation Graph/)).toBeInTheDocument();
    expect(screen.getByText(/Groups/)).toBeInTheDocument();
    expect(screen.getByText(/2024-09-29/)).toBeInTheDocument();
  });

  it('labels the sectors scope when provided', () => {
    renderWithProviders(<RRGChart data={{ ...sampleData, scope: 'sectors' }} />);
    expect(screen.getByText(/Sectors/)).toBeInTheDocument();
  });

  it('renders a current-rank range slider (two thumbs)', () => {
    renderWithProviders(<RRGChart data={sampleData} />);
    expect(screen.getByText(/Rank 1/)).toBeInTheDocument();
    expect(screen.getAllByRole('slider')).toHaveLength(2);
  });

  it('renders a multi-select quadrant filter (one toggle per quadrant)', () => {
    renderWithProviders(<RRGChart data={sampleData} />);
    ['Improving', 'Leading', 'Weakening', 'Lagging'].forEach((q) => {
      expect(screen.getByRole('button', { name: q })).toBeInTheDocument();
    });
  });

  it('renders a Labels toggle that starts off and can be turned on', () => {
    renderWithProviders(<RRGChart data={sampleData} />);
    const toggle = screen.getByRole('checkbox', { name: /labels/i });
    expect(toggle).not.toBeChecked();
    fireEvent.click(toggle);
    expect(toggle).toBeChecked();
  });

  it('renders a scope-aware filter control', () => {
    const { rerender } = renderWithProviders(<RRGChart data={sampleData} />);
    expect(screen.getByLabelText(/Filter groups/i)).toBeInTheDocument();
    rerender(<RRGChart data={{ ...sampleData, scope: 'sectors' }} />);
    expect(screen.getByLabelText(/Filter sectors/i)).toBeInTheDocument();
  });

  it('shows a spinner while loading', () => {
    renderWithProviders(<RRGChart data={null} isLoading />);
    expect(screen.getByRole('progressbar')).toBeInTheDocument();
  });

  it('shows an error alert on failure', () => {
    renderWithProviders(<RRGChart data={null} error={{ message: 'boom' }} />);
    expect(screen.getByText(/Failed to load RRG data/)).toBeInTheDocument();
    expect(screen.getByText(/boom/)).toBeInTheDocument();
  });

  it('shows an empty-state message when there are no groups', () => {
    renderWithProviders(<RRGChart data={{ groups: [] }} />);
    expect(screen.getByText(/No RRG data available/)).toBeInTheDocument();
  });

  it('keeps each quadrant on its theme tone in both modes', () => {
    // Guards the live + static charts against colour drift.
    expect(QUADRANT_TONES).toEqual({
      Leading: 'success',
      Weakening: 'warning',
      Lagging: 'error',
      Improving: 'primary',
    });
    for (const mode of ['light', 'dark']) {
      const theme = createTheme(getDesignTokens(mode));
      const { result } = renderHook(() => useQuadrantColors(), {
        wrapper: ({ children }) => <ThemeProvider theme={theme}>{children}</ThemeProvider>,
      });
      expect(result.current.colors.Leading).toBe(theme.palette.success.main);
      expect(result.current.colorOf('Unknown')).toBe(theme.palette.text.disabled);
    }
  });
});
