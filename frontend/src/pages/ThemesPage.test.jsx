import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ThemesPage from './ThemesPage';
import { renderWithProviders } from '../test/renderWithProviders';

const economicApi = vi.hoisted(() => ({
  getEconomicThemes: vi.fn(),
  getEconomicTaxonomyReview: vi.fn(),
}));

const runtimeState = {
  runtimeReady: true,
  features: { social_signals: false },
};

vi.mock('../contexts/RuntimeContext', () => ({
  useRuntime: () => runtimeState,
}));

vi.mock('../contexts/usePipeline', () => ({
  usePipeline: () => ({
    isPipelineRunning: false,
    startPipeline: vi.fn(),
  }),
}));

vi.mock('../api/economicThemes', () => economicApi);

vi.mock('../components/Themes/ThemeTaxonomyTable', () => ({
  default: ({ pipeline, categoryFilter }) => (
    <div data-testid="taxonomy">
      {pipeline}:{categoryFilter ?? 'none'}
    </div>
  ),
}));

vi.mock('../components/Themes/ThemeReviewDialog', () => ({
  default: ({ open }) => (open ? <h2>Theme Review</h2> : null),
}));

vi.mock('../components/Themes/ModelSettingsModal', () => ({
  default: () => null,
}));

vi.mock('../components/Themes/ManageSourcesModal', () => ({
  default: ({ open }) => (open ? <h2>Manage Content Sources</h2> : null),
  ManageSourcesContent: () => null,
}));

vi.mock('../features/socialSignals/SocialThemePulse', () => ({
  default: ({ enabled }) => enabled ? <div>Published Social Pulse</div> : null,
}));

vi.mock('../api/themes', () => {
  const getThemeRankings = vi.fn().mockResolvedValue({
    date: '2026-04-09',
    total_themes: 1,
    rankings: [
      {
        theme_cluster_id: 1,
        theme: 'AI Infrastructure',
        rank: 1,
        momentum_score: 84,
        mention_velocity: 1.7,
        mentions_7d: 18,
        basket_rs_vs_spy: 72,
        basket_return_1w: 3.8,
        pct_above_50ma: 78,
        num_constituents: 4,
        top_tickers: ['NVDA', 'AVGO'],
        status: 'trending',
      },
    ],
  });

  return {
    dismissAlert: vi.fn(),
    getAlerts: vi.fn().mockResolvedValue({ total: 0, unread: 0, alerts: [] }),
    getCandidateThemeQueue: vi.fn().mockResolvedValue({ total: 0, items: [] }),
    getEmergingThemes: vi.fn().mockResolvedValue({ count: 0, themes: [] }),
    getFailedItemsCount: vi.fn().mockResolvedValue({ failed_count: 0 }),
    getL1Categories: vi.fn().mockResolvedValue({
      categories: [{ category: 'technology', count: 1 }],
    }),
    getL1Rankings: vi.fn().mockResolvedValue({
      rankings: [{ id: 100, display_name: 'Technology', rank: 1, momentum_score: 80 }],
    }),
    getMergeSuggestions: vi.fn().mockResolvedValue({ total: 0, suggestions: [] }),
    getPipelineObservability: vi.fn().mockResolvedValue({ metrics: {}, alerts: [] }),
    getThemeRankings,
    getThemesBootstrap: vi.fn().mockRejectedValue(new Error('no snapshot')),
    runPipelineAsync: vi.fn(),
    getThemeDetail: vi.fn(),
    getThemeHistory: vi.fn(),
    getThemeRelationshipGraph: vi.fn(),
    getThemeMentions: vi.fn(),
  };
});

function renderPage() {
  return renderWithProviders(<ThemesPage />);
}

describe('ThemesPage', () => {
  beforeEach(() => {
    localStorage.clear();
    runtimeState.runtimeReady = true;
    runtimeState.features = { social_signals: false };
    economicApi.getEconomicThemes.mockReset();
    economicApi.getEconomicThemes.mockRejectedValue(new Error('not configured'));
    economicApi.getEconomicTaxonomyReview.mockReset();
    economicApi.getEconomicTaxonomyReview.mockResolvedValue(null);
  });

  it('resets grouped category filter when pipeline toggles', async () => {
    renderPage();
    expect(await screen.findByTestId('taxonomy', {}, { timeout: 5000 })).toHaveTextContent('technical:none');

    await screen.findByText(/technology \(1\)/i, {}, { timeout: 5000 });
    fireEvent.click(screen.getByRole('button', { name: /technology \(1\)/i }));
    expect(screen.getByTestId('taxonomy')).toHaveTextContent('technical:technology');

    fireEvent.click(screen.getByRole('button', { name: /fundamental/i }));
    expect(await screen.findByTestId('taxonomy', {}, { timeout: 5000 })).toHaveTextContent('fundamental:none');
  });

  it('supports flat-view switch and opens review surface', async () => {
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'All Themes' }));

    await waitFor(() => {
      expect(screen.getByText('Theme Rankings')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Review' }));
    expect(screen.getByText('Theme Review')).toBeInTheDocument();
  });

  it('adds display-only Social Pulse without changing the Theme ranking surface', async () => {
    runtimeState.features = { social_signals: true };
    renderPage();
    expect(await screen.findByText('Published Social Pulse')).toBeInTheDocument();
    expect(await screen.findByTestId('taxonomy')).toHaveTextContent('technical:none');
  });

  it('uses the generation-scoped global view in economic authority mode', async () => {
    economicApi.getEconomicThemes.mockResolvedValue({
      generation_id: 'generation-1',
      generation: { authority_mode: 'economic' },
      themes: [{
        economic_theme_id: 'theme-1',
        display_name: 'AI Memory',
        lifecycle: 'active',
        definition: 'AI-driven memory demand.',
        metrics: {
          technical_attention: { availability: 'available', percentile: 92 },
          fundamental_attention: { availability: 'unavailable' },
          narrative_attention: { availability: 'available', percentile: 84 },
          emerging: { availability: 'available', percentile: 77 },
          broad_confirmation: { availability: 'available', percentile: 89 },
        },
      }],
    });
    economicApi.getEconomicTaxonomyReview.mockResolvedValue({
      generation_id: 'generation-1',
      allocations: [],
      reconciliation: [],
      operation_previews: [],
    });

    renderPage();

    expect(await screen.findByRole('heading', { name: 'Economic Themes' })).toBeInTheDocument();
    expect(screen.getByText('AI Memory')).toBeInTheDocument();
    expect(screen.getByText('Fundamental Attention')).toBeInTheDocument();
    expect(screen.getByText('Unavailable')).toBeInTheDocument();
    expect(screen.queryByText('Fundamental Momentum')).not.toBeInTheDocument();
    expect(screen.queryByTestId('taxonomy')).not.toBeInTheDocument();
  });

  it('keeps content-source management reachable in economic authority mode', async () => {
    // #471: ingested sources still feed economic evidence after cutover.
    economicApi.getEconomicThemes.mockResolvedValue({
      generation_id: 'generation-1',
      generation: { authority_mode: 'economic' },
      themes: [],
    });

    renderPage();

    fireEvent.click(await screen.findByRole('button', { name: 'Content sources' }));
    expect(screen.getByRole('heading', { name: 'Manage Content Sources' })).toBeInTheDocument();
  });

  it('switches to a newly published economic generation while mounted', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    economicApi.getEconomicThemes
      .mockResolvedValueOnce({
        generation_id: 'generation-legacy',
        generation: { authority_mode: 'dual' },
        themes: [],
      })
      .mockResolvedValueOnce({
        generation_id: 'generation-economic',
        generation: { authority_mode: 'economic' },
        themes: [{
          economic_theme_id: 'theme-1',
          display_name: 'AI Memory',
          lifecycle: 'active',
          definition: 'AI-driven memory demand.',
          metrics: {},
        }],
      });

    const { unmount } = renderPage();
    expect(await screen.findByTestId('taxonomy')).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });

    await waitFor(() => expect(economicApi.getEconomicThemes).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole('heading', { name: 'Economic Themes' })).toBeInTheDocument();
    expect(screen.queryByTestId('taxonomy')).not.toBeInTheDocument();

    unmount();
    vi.useRealTimers();
  });

  it('refreshes an open economic-theme detail with the new generation', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    economicApi.getEconomicThemes
      .mockResolvedValueOnce({
        generation_id: 'generation-1',
        generation: { authority_mode: 'economic' },
        themes: [{
          economic_theme_id: 'theme-1',
          display_name: 'AI Memory',
          lifecycle: 'active',
          definition: 'Original definition.',
          metrics: {},
        }],
      })
      .mockResolvedValueOnce({
        generation_id: 'generation-2',
        generation: { authority_mode: 'economic' },
        themes: [{
          economic_theme_id: 'theme-1',
          display_name: 'AI Memory Infrastructure',
          lifecycle: 'established',
          definition: 'Updated definition.',
          metrics: {},
        }],
      });

    const { unmount } = renderPage();
    fireEvent.click(await screen.findByText('AI Memory'));
    expect(screen.getByRole('dialog')).toHaveTextContent('Generation generation-1');
    expect(screen.getByRole('dialog')).toHaveTextContent('Original definition.');

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });

    await waitFor(() => {
      expect(screen.getByRole('dialog')).toHaveTextContent(
        'AI Memory Infrastructure',
      );
    });
    expect(screen.getByRole('dialog')).toHaveTextContent('Generation generation-2');
    expect(screen.getByRole('dialog')).toHaveTextContent('Updated definition.');
    expect(screen.getByRole('dialog')).not.toHaveTextContent('Original definition.');

    unmount();
    vi.useRealTimers();
  });
});
