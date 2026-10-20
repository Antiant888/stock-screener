import apiClient from './client';

const generationParams = (generationId) => (
  generationId ? { generation_id: generationId } : {}
);

export const getEconomicThemes = async ({ generationId } = {}) => {
  const response = await apiClient.get('/v1/economic-themes', {
    params: generationParams(generationId),
  });
  return response.data;
};

export const getEconomicTaxonomyReview = async ({ generationId } = {}) => {
  const response = await apiClient.get('/v1/economic-taxonomy/review', {
    params: generationParams(generationId),
  });
  return response.data;
};
