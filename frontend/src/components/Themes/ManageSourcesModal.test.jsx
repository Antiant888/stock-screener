import { screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { renderWithProviders } from '../../test/renderWithProviders';
import { ManageSourcesContent } from './ManageSourcesModal';
import * as themesApi from '../../api/themes';

vi.mock('../../api/themes', () => ({
  getContentSources: vi.fn(), addContentSource: vi.fn(),
  updateContentSource: vi.fn(), deleteContentSource: vi.fn(),
}));

describe('ManageSourcesContent Social ownership', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    themesApi.getContentSources.mockResolvedValue([
      { id: 1, name: 'Minervini Research List', source_type: 'twitter',
        url: 'https://x.com/i/lists/1522014550211457024', pipelines: ['technical'],
        priority: 50, fetch_interval_minutes: 360, is_active: true,
        last_fetched_at: null, total_items_fetched: 0, social_managed: true },
      { id: 2, name: 'Ordinary News', source_type: 'news', url: 'https://example.com',
        pipelines: ['technical'], priority: 50, fetch_interval_minutes: 60,
        is_active: true, last_fetched_at: null, total_items_fetched: 1,
        social_managed: false },
    ]);
  });

  it('removes legacy mutations for Social rows and keeps ordinary controls', async () => {
    renderWithProviders(<ManageSourcesContent />);
    const socialRow = (await screen.findByText('Minervini Research List')).closest('tr');
    expect(within(socialRow).getByText('Managed in Operations → Social Sources')).toBeInTheDocument();
    expect(within(socialRow).queryByRole('button')).not.toBeInTheDocument();
    const ordinaryRow = screen.getByText('Ordinary News').closest('tr');
    expect(within(ordinaryRow).getAllByRole('button')).toHaveLength(2);
  });
});

describe('ManageSourcesContent fetch health', () => {
  it('flags a source whose recent fetches failed and keeps healthy sources unflagged', async () => {
    themesApi.getContentSources.mockResolvedValue([
      { id: 3, name: 'Broken Feed', source_type: 'rss', url: 'https://example.com/feed',
        pipelines: ['technical'], priority: 50, fetch_interval_minutes: 60, is_active: true,
        last_fetched_at: null, last_success_at: null, total_items_fetched: 0,
        consecutive_failures: 4, last_error: 'HTTPError: 503 Server Error',
        last_error_at: '2026-10-15T08:00:00Z', social_managed: false },
      { id: 4, name: 'Healthy Feed', source_type: 'rss', url: 'https://example.com/ok',
        pipelines: ['technical'], priority: 50, fetch_interval_minutes: 60, is_active: true,
        last_fetched_at: '2026-10-15T08:00:00Z', last_success_at: '2026-10-15T08:00:00Z',
        total_items_fetched: 9, consecutive_failures: 0, last_error: null,
        last_error_at: null, social_managed: false },
    ]);

    renderWithProviders(<ManageSourcesContent />);

    const brokenRow = (await screen.findByText('Broken Feed')).closest('tr');
    expect(within(brokenRow).getByText('Failed 4× in a row')).toBeInTheDocument();
    expect(within(brokenRow).getByLabelText(/HTTPError: 503 Server Error/)).toBeInTheDocument();
    const healthyRow = screen.getByText('Healthy Feed').closest('tr');
    expect(within(healthyRow).queryByText(/in a row/)).not.toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'Last Success' })).toBeInTheDocument();
  });
});
