/**
 * API client for telemetry alerts and opportunity-evidence open events.
 */
import apiClient from './client';

/**
 * Active alerts (open + acknowledged). When ``evaluate`` is true (default),
 * the backend re-evaluates thresholds before returning so the result reflects
 * current gauge state.
 */
export const fetchAlerts = async ({ evaluate = true } = {}) => {
  const response = await apiClient.get('/v1/telemetry/alerts', { params: { evaluate } });
  return response.data;
};

/** Acknowledge an open alert. ``acknowledgedBy`` is recorded on the row. */
export const acknowledgeAlert = async (alertId, acknowledgedBy) => {
  const response = await apiClient.post(
    `/v1/telemetry/alerts/${alertId}/acknowledge`,
    { acknowledged_by: acknowledgedBy },
  );
  return response.data;
};

/** Record one privacy-safe live opportunity-evidence open. */
export const postOpportunityEvidenceOpen = async (market, surface) => {
  await apiClient.post('/v1/telemetry/opportunity/evidence-open', { market, surface });
};
