import { beforeEach, describe, expect, it, vi } from 'vitest';

import apiClient from './client';
import {
  getEconomicTaxonomyReview,
  getEconomicThemes,
} from './economicThemes';

vi.mock('./client', () => ({ default: { get: vi.fn() } }));

describe('Economic Theme API', () => {
  beforeEach(() => vi.clearAllMocks());

  it('reads the current generation when no generation is supplied', async () => {
    apiClient.get.mockResolvedValue({ data: { themes: [] } });

    await getEconomicThemes();
    await getEconomicTaxonomyReview();

    expect(apiClient.get.mock.calls).toEqual([
      ['/v1/economic-themes', { params: {} }],
      ['/v1/economic-taxonomy/review', { params: {} }],
    ]);
  });

  it('pins catalog and review requests to one generation', async () => {
    apiClient.get.mockResolvedValue({ data: {} });

    await getEconomicThemes({ generationId: 'generation-1' });
    await getEconomicTaxonomyReview({ generationId: 'generation-1' });

    expect(apiClient.get.mock.calls).toEqual([
      ['/v1/economic-themes', { params: { generation_id: 'generation-1' } }],
      ['/v1/economic-taxonomy/review', {
        params: { generation_id: 'generation-1' },
      }],
    ]);
  });
});
