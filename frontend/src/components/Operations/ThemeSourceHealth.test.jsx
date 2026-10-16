import { screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { renderWithProviders } from '../../test/renderWithProviders';
import ThemeSourceHealth from './ThemeSourceHealth';
import { failingSources } from './contentSources';

const getContentSources = vi.fn();

vi.mock('../../api/themes', () => ({
  getContentSources: (...args) => getContentSources(...args),
}));

const source = (overrides) => ({
  id: 1,
  name: 'Feed',
  source_type: 'rss',
  is_active: true,
  social_managed: false,
  last_success_at: null,
  last_fetched_at: null,
  consecutive_failures: 0,
  last_error: null,
  last_error_at: null,
  ...overrides,
});

describe('ThemeSourceHealth', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('lists failing sources, worst first, with failure count and error', async () => {
    getContentSources.mockResolvedValue([
      source({ id: 1, name: 'Mild Feed', consecutive_failures: 1, last_error: 'timeout' }),
      source({ id: 2, name: 'Broken Feed', source_type: 'podcast', consecutive_failures: 3, last_error: '403 Forbidden' }),
      source({ id: 3, name: 'Fine Feed' }),
    ]);
    renderWithProviders(<ThemeSourceHealth />);

    expect(await screen.findByText('3 active · 2 failing')).toBeInTheDocument();
    const rows = screen.getAllByRole('row').slice(1);
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByText('Broken Feed')).toBeInTheDocument();
    expect(within(rows[0]).getByText('podcast')).toBeInTheDocument();
    expect(within(rows[0]).getByText('3× in a row')).toBeInTheDocument();
    expect(within(rows[0]).getByText('403 Forbidden')).toBeInTheDocument();
    expect(within(rows[0]).getByText('Never')).toBeInTheDocument();
    expect(within(rows[1]).getByText('Mild Feed')).toBeInTheDocument();
    expect(within(rows[1]).getByText('1× in a row')).toBeInTheDocument();
    expect(screen.queryByText('Fine Feed')).not.toBeInTheDocument();
    expect(getContentSources).toHaveBeenCalledWith(false);
  });

  it('ignores inactive and social-managed sources', async () => {
    getContentSources.mockResolvedValue([
      source({ id: 1, name: 'Active', consecutive_failures: 2, last_error: 'boom' }),
      source({ id: 2, name: 'Paused', is_active: false, consecutive_failures: 9 }),
      source({ id: 3, name: 'X Account', social_managed: true, consecutive_failures: 9 }),
    ]);
    renderWithProviders(<ThemeSourceHealth />);

    expect(await screen.findByText('1 active · 1 failing')).toBeInTheDocument();
    expect(screen.getByText('Active')).toBeInTheDocument();
    expect(screen.queryByText('Paused')).not.toBeInTheDocument();
    expect(screen.queryByText('X Account')).not.toBeInTheDocument();
  });

  it('says all healthy and shows no table when nothing is failing', async () => {
    getContentSources.mockResolvedValue([source({ id: 1 }), source({ id: 2 })]);
    renderWithProviders(<ThemeSourceHealth />);

    expect(await screen.findByText('2 active · all healthy')).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('says when the sources cannot be loaded', async () => {
    getContentSources.mockRejectedValue(new Error('down'));
    renderWithProviders(<ThemeSourceHealth />);

    expect(await screen.findByText('Failed to load theme content sources.')).toBeInTheDocument();
  });
});

describe('failingSources', () => {
  it('returns [] for no data', () => {
    expect(failingSources(undefined)).toEqual([]);
  });
});
