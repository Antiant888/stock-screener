import apiClient from './client';

export const fetchOperationsJobs = async () => {
  const response = await apiClient.get('/v1/operations/jobs');
  return response.data;
};

// Frees a market's daily pipeline chain claim only if `token` still holds it.
export const releasePipelineChain = async (market, token) => {
  const response = await apiClient.post(`/v1/operations/pipeline-chains/${market}/release`, { token });
  return response.data;
};

// expectedStrategy: the action the operator confirmed; the server returns 409 if it no longer applies.
export const cancelOperationsJob = async (taskId, expectedStrategy) => {
  const response = await apiClient.post(`/v1/operations/jobs/${taskId}/cancel`, {
    expected_strategy: expectedStrategy,
  });
  return response.data;
};

