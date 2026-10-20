/**
 * API client for Theme Discovery endpoints.
 */
import apiClient from './client';
import {
  adaptCandidateQueueResponse,
  adaptCandidateReviewResponse,
  adaptMergeActionResponse,
  adaptMergeHistoryResponse,
  adaptMergeSuggestionsResponse,
  adaptRelationshipGraphResponse,
} from './themeAdapters';

/**
 * Get the published themes bootstrap snapshot for a pipeline/view default state.
 *
 * @param {Object} params - Snapshot selector
 * @param {string} [params.pipeline='technical'] - technical|fundamental
 * @param {string} [params.themeView='grouped'] - grouped|flat
 * @returns {Promise<Object>} Snapshot envelope
 */
export const getThemesBootstrap = async ({ pipeline = 'technical', themeView = 'grouped' } = {}) => {
  const response = await apiClient.get('/v1/themes/bootstrap', {
    params: { pipeline, theme_view: themeView },
  });
  return response.data;
};

/**
 * Get current theme rankings.
 *
 * @param {number} limit - Maximum number of themes to return (default: 20)
 * @param {string|null} status - Filter by status: emerging, trending, fading, dormant
 * @param {string[]|null} sourceTypes - Filter by source types: substack, twitter, news, reddit
 * @param {string} pipeline - Pipeline: technical or fundamental (default: 'technical')
 * @param {number} offset - Number of themes to skip for pagination (default: 0)
 * @returns {Promise<Object>} Rankings response with date, total_themes, pipeline, and rankings array
 */
export const getThemeRankings = async (limit = 20, status = null, sourceTypes = null, pipeline = 'technical', offset = 0) => {
  const params = { limit, pipeline, offset };
  if (status) params.status = status;
  if (sourceTypes?.length) params.source_types = sourceTypes.join(',');

  const response = await apiClient.get('/v1/themes/rankings', { params });
  return response.data;
};

/**
 * Get emerging themes (newly discovered with accelerating mentions).
 *
 * @param {number} minVelocity - Minimum mention velocity (default: 1.5)
 * @param {number} minMentions - Minimum mentions in 7 days (default: 3)
 * @param {string} pipeline - Pipeline: technical or fundamental (default: 'technical')
 * @returns {Promise<Object>} Object with count and themes array
 */
export const getEmergingThemes = async (minVelocity = 1.5, minMentions = 3, pipeline = 'technical') => {
  const response = await apiClient.get('/v1/themes/emerging', {
    params: { min_velocity: minVelocity, min_mentions: minMentions, pipeline }
  });
  return response.data;
};

/**
 * Get detailed information about a specific theme.
 *
 * @param {number} themeId - Theme cluster ID
 * @returns {Promise<Object>} Theme detail with constituents and metrics
 */
export const getThemeDetail = async (themeId) => {
  const response = await apiClient.get(`/v1/themes/${themeId}`);
  return response.data;
};

/**
 * Get historical metrics for a theme.
 *
 * @param {number} themeId - Theme cluster ID
 * @param {number} days - Days of history (default: 30)
 * @returns {Promise<Object>} Theme history with metrics over time
 */
export const getThemeHistory = async (themeId, days = 30) => {
  const response = await apiClient.get(`/v1/themes/${themeId}/history`, {
    params: { days }
  });
  return response.data;
};

/**
 * Get news mentions for a specific theme.
 *
 * @param {number} themeId - Theme cluster ID
 * @param {number} limit - Maximum mentions to return (default: 50)
 * @returns {Promise<Object>} Theme mentions with content details
 */
export const getThemeMentions = async (themeId, limit = 50) => {
  const response = await apiClient.get(`/v1/themes/${themeId}/mentions`, {
    params: { limit }
  });
  return response.data;
};

/**
 * Get theme alerts.
 *
 * @param {boolean} unreadOnly - Only return unread alerts (default: false)
 * @param {number} limit - Maximum alerts to return (default: 50)
 * @returns {Promise<Object>} Alerts response
 */
export const getAlerts = async (unreadOnly = false, limit = 50) => {
  const response = await apiClient.get('/v1/themes/alerts', {
    params: { unread_only: unreadOnly, limit }
  });
  return response.data;
};

/**
 * Dismiss (soft delete) an alert.
 *
 * @param {number} alertId - Alert ID
 * @returns {Promise<Object>} Status response
 */
export const dismissAlert = async (alertId) => {
  const response = await apiClient.post(`/v1/themes/alerts/${alertId}/dismiss`);
  return response.data;
};

/**
 * List content sources.
 *
 * @param {boolean} activeOnly - Only return active sources (default: true)
 * @param {string|null} pipeline - Filter by pipeline (optional)
 * @returns {Promise<Array>} List of content sources
 */
export const getContentSources = async (activeOnly = true, pipeline = null) => {
  const params = { active_only: activeOnly };
  if (pipeline) params.pipeline = pipeline;
  const response = await apiClient.get('/v1/themes/sources', { params });
  return response.data;
};

/**
 * Add a new content source.
 *
 * @param {Object} source - Source configuration
 * @returns {Promise<Object>} Created source
 */
export const addContentSource = async (source) => {
  const response = await apiClient.post('/v1/themes/sources', source);
  return response.data;
};

/**
 * Update an existing content source.
 *
 * @param {number} sourceId - Source ID
 * @param {Object} updates - Fields to update
 * @returns {Promise<Object>} Updated source
 */
export const updateContentSource = async (sourceId, updates) => {
  const response = await apiClient.put(`/v1/themes/sources/${sourceId}`, updates);
  return response.data;
};

/**
 * Deactivate a content source.
 *
 * @param {number} sourceId - Source ID
 * @returns {Promise<Object>} Status response
 */
export const deleteContentSource = async (sourceId) => {
  const response = await apiClient.delete(`/v1/themes/sources/${sourceId}`);
  return response.data;
};

/**
 * Create a theme from selected stocks.
 *
 * @param {string} name - Theme name
 * @param {Array<string>} symbols - Stock symbols
 * @param {string|null} description - Optional description
 * @returns {Promise<Object>} Created theme
 */
export const createTheme = async (name, symbols, description = null) => {
  const response = await apiClient.post('/v1/themes/create-from-cluster', null, {
    params: { name, symbols, description }
  });
  return response.data;
};

// ==================== Async Pipeline (Celery-based) ====================

/**
 * Start the full theme discovery pipeline asynchronously.
 *
 * This queues a Celery task that runs:
 * 1. Content ingestion from all active sources
 * 2. Theme extraction via LLM
 * 3. Metrics calculation for all themes
 * 4. Alert generation
 *
 * @param {string|null} pipeline - Pipeline: technical, fundamental, or null for both
 * @returns {Promise<Object>} Response with run_id and task_id for tracking
 */
export const runPipelineAsync = async (pipeline = null, lookbackDays = null) => {
  const params = {};
  if (pipeline) params.pipeline = pipeline;
  if (lookbackDays) params.lookback_days = lookbackDays;
  const response = await apiClient.post('/v1/themes/pipeline/run', null, { params });
  return response.data;
};

/**
 * Get status of a pipeline run.
 *
 * Poll this endpoint to track progress of an async pipeline run.
 *
 * @param {string} runId - Pipeline run ID from runPipelineAsync()
 * @returns {Promise<Object>} Pipeline status with progress info
 */
export const getPipelineStatus = async (runId) => {
  const response = await apiClient.get(`/v1/themes/pipeline/${runId}/status`);
  return response.data;
};

/**
 * Get count of content items with extraction errors eligible for retry.
 *
 * @param {string|null} pipeline - Filter by pipeline (optional)
 * @returns {Promise<Object>} Object with failed_count and max_age_days
 */
export const getFailedItemsCount = async (pipeline = null) => {
  const params = {};
  if (pipeline) params.pipeline = pipeline;
  const response = await apiClient.get('/v1/themes/pipeline/failed-count', { params });
  return response.data;
};

/**
 * Get observability dashboard metrics and actionable alerts for a pipeline.
 *
 * @param {string} pipeline - technical or fundamental
 * @param {number} windowDays - Lookback window (default: 30)
 * @returns {Promise<Object>} Observability payload
 */
export const getPipelineObservability = async (pipeline = 'technical', windowDays = 30) => {
  const response = await apiClient.get('/v1/themes/pipeline/observability', {
    params: { pipeline, window_days: windowDays },
  });
  return response.data;
};

// ==================== Theme Merge Operations ====================

/**
 * Get merge suggestions with optional status filter.
 *
 * @param {string} status - Filter by status: pending, approved, rejected (default: 'pending')
 * @param {number} limit - Maximum suggestions to return (default: 50)
 * @returns {Promise<Object>} List of merge suggestions
 */
export const getMergeSuggestions = async (status = 'pending', limit = 50) => {
  const response = await apiClient.get('/v1/themes/merge-suggestions', {
    params: { status, limit }
  });
  return adaptMergeSuggestionsResponse(response.data);
};

/**
 * Approve a merge suggestion (executes the merge).
 *
 * @param {number} suggestionId - Merge suggestion ID
 * @returns {Promise<Object>} Result of the merge operation
 */
export const approveMergeSuggestion = async (suggestionId) => {
  const response = await apiClient.post(`/v1/themes/merge-suggestions/${suggestionId}/approve`);
  return adaptMergeActionResponse(response.data);
};

/**
 * Reject a merge suggestion.
 *
 * @param {number} suggestionId - Merge suggestion ID
 * @returns {Promise<Object>} Result of the rejection
 */
export const rejectMergeSuggestion = async (suggestionId) => {
  const response = await apiClient.post(`/v1/themes/merge-suggestions/${suggestionId}/reject`);
  return adaptMergeActionResponse(response.data);
};

/**
 * Get merge history.
 *
 * @param {number} limit - Maximum history entries to return (default: 50)
 * @returns {Promise<Object>} List of merge history entries
 */
export const getMergeHistory = async (limit = 50) => {
  const response = await apiClient.get('/v1/themes/merge-history', {
    params: { limit }
  });
  return adaptMergeHistoryResponse(response.data);
};

/**
 * Get candidate-theme analyst review queue.
 *
 * @param {Object} params - Optional query filters
 * @param {number} [params.limit=100] - Max queue rows
 * @param {number} [params.offset=0] - Pagination offset
 * @param {string} [params.pipeline='technical'] - technical|fundamental
 * @returns {Promise<Object>} Normalized candidate queue payload
 */
export const getCandidateThemeQueue = async ({
  limit = 100,
  offset = 0,
  pipeline = 'technical',
} = {}) => {
  const response = await apiClient.get('/v1/themes/candidates/queue', {
    params: { limit, offset, pipeline },
  });
  return adaptCandidateQueueResponse(response.data);
};

/**
 * Submit candidate-theme review action(s).
 *
 * @param {Object} payload - Review payload
 * @param {Array<number>} payload.theme_cluster_ids - Candidate IDs
 * @param {string} payload.action - promote|reject
 * @param {string} [payload.actor='analyst'] - Optional reviewer id
 * @param {string|null} [payload.note=null] - Optional note
 * @param {string} [pipeline='technical'] - technical|fundamental
 * @returns {Promise<Object>} Normalized review result payload
 */
export const reviewCandidateThemes = async (payload, pipeline = 'technical') => {
  const response = await apiClient.post('/v1/themes/candidates/review', payload, {
    params: { pipeline },
  });
  return adaptCandidateReviewResponse(response.data);
};

/**
 * Fetch relationship graph centered on a theme.
 *
 * @param {number} themeClusterId - Root theme id
 * @param {Object} params - Optional query params
 * @param {string} [params.pipeline='technical'] - technical|fundamental
 * @param {number} [params.limit=120] - Max graph edges/nodes scope
 * @returns {Promise<Object>} Normalized relationship graph payload
 */
export const getThemeRelationshipGraph = async (themeClusterId, {
  pipeline = 'technical',
  limit = 120,
} = {}) => {
  const response = await apiClient.get('/v1/themes/relationship-graph', {
    params: { theme_cluster_id: themeClusterId, pipeline, limit },
  });
  return adaptRelationshipGraphResponse(response.data);
};

/**
 * Run theme consolidation to find duplicate themes.
 *
 * This generates new merge suggestions by:
 * 1. Updating embeddings for all themes
 * 2. Finding similar pairs via cosine similarity
 * 3. Verifying with LLM
 *
 * @param {boolean} dryRun - If true, only preview without executing (default: false)
 * @returns {Promise<Object>} Task info with task_id for polling
 */
export const runThemeConsolidation = async (dryRun = false) => {
  const response = await apiClient.post('/v1/themes/consolidate/async', null, {
    params: { dry_run: dryRun }
  });
  return response.data;
};

// ==================== L1/L2 Taxonomy ====================

/**
 * Get L1 theme rankings with aggregated metrics.
 *
 * @param {Object} params - Query parameters
 * @param {string} [params.pipeline='technical'] - Pipeline filter
 * @param {string|null} [params.category=null] - Filter by L1 category
 * @param {number} [params.limit=100] - Max themes
 * @param {number} [params.offset=0] - Pagination offset
 * @returns {Promise<Object>} L1 rankings response
 */
export const getL1Rankings = async ({ pipeline = 'technical', category = null, limit = 100, offset = 0, sortBy = 'momentum_score', sortOrder = 'desc' } = {}) => {
  const params = { pipeline, limit, offset, sort_by: sortBy, sort_order: sortOrder };
  if (category) params.category = category;
  const response = await apiClient.get('/v1/themes/taxonomy/l1', { params });
  return response.data;
};

/**
 * Get L2 children of an L1 theme.
 *
 * @param {number} l1Id - L1 theme cluster ID
 * @param {Object} params - Query parameters
 * @param {number} [params.limit=100] - Max children
 * @param {number} [params.offset=0] - Pagination offset
 * @returns {Promise<Object>} L1 with children response
 */
export const getL1Children = async (l1Id, { limit = 100, offset = 0, sortBy = 'momentum_score', sortOrder = 'desc' } = {}) => {
  const response = await apiClient.get(`/v1/themes/taxonomy/l1/${l1Id}/children`, {
    params: { limit, offset, sort_by: sortBy, sort_order: sortOrder },
  });
  return response.data;
};

/**
 * Get available L1 categories with counts.
 *
 * @param {string} [pipeline='technical'] - Pipeline filter
 * @returns {Promise<Object>} Categories response
 */
export const getL1Categories = async (pipeline = 'technical') => {
  const response = await apiClient.get('/v1/themes/taxonomy/categories', {
    params: { pipeline },
  });
  return response.data;
};

// ==================== Content Item Browser ====================

/**
 * Get paginated list of content items with themes.
 *
 * @param {Object} params - Query parameters
 * @param {string} params.search - Search in title, source_name, tickers
 * @param {string} params.source_type - Filter by source type
 * @param {string} params.sentiment - Filter by sentiment
 * @param {string} params.pipeline - Pipeline filter: technical or fundamental
 * @param {string} params.date_from - From date (YYYY-MM-DD)
 * @param {string} params.date_to - To date (YYYY-MM-DD)
 * @param {number} params.limit - Items per page (default: 50)
 * @param {number} params.offset - Pagination offset (default: 0)
 * @param {string} params.sort_by - Sort column (default: published_at)
 * @param {string} params.sort_order - Sort order: asc or desc (default: desc)
 * @returns {Promise<Object>} Paginated content items with themes and optional processing_status
 */
export const getContentItems = async (params = {}) => {
  const response = await apiClient.get('/v1/themes/content', { params });
  return response.data;
};

/**
 * Export content items matching filters as CSV.
 *
 * @param {Object} params - Same filter params as getContentItems (without limit/offset); supports pipeline
 * @returns {Promise<Blob>} CSV file blob
 */
export const exportContentItems = async (params = {}) => {
  const response = await apiClient.get('/v1/themes/content/export', {
    params,
    responseType: 'blob',
  });
  return response.data;
};

// ==================== LLM Configuration ====================

/**
 * Get current LLM configuration.
 *
 * @returns {Promise<Object>} LLM config with current models and Ollama status
 */
export const getLLMConfig = async () => {
  const response = await apiClient.get('/v1/config/llm');
  return response.data;
};

/**
 * Update LLM model selection.
 *
 * @param {string} modelId - The model ID to use (e.g., "groq/llama-3.3-70b-versatile")
 * @param {string} useCase - The use case: "extraction" or "merge"
 * @returns {Promise<Object>} Updated configuration
 */
export const updateLLMModel = async (modelId, useCase = 'extraction') => {
  const response = await apiClient.post('/v1/config/llm', {
    model_id: modelId,
    use_case: useCase,
  });
  return response.data;
};

/**
 * Update Ollama API settings.
 *
 * @param {string} apiBase - Ollama API base URL
 * @returns {Promise<Object>} Updated Ollama configuration
 */
export const updateOllamaSettings = async (apiBase) => {
  const response = await apiClient.post('/v1/config/ollama', {
    api_base: apiBase,
  });
  return response.data;
};

/**
 * Get available Ollama models.
 *
 * @returns {Promise<Object>} List of installed Ollama models
 */
export const getOllamaModels = async () => {
  const response = await apiClient.get('/v1/config/ollama/models');
  return response.data;
};

/**
 * Get theme policy config (defaults/overrides/effective/history) for a pipeline.
 *
 * @param {string} pipeline - technical|fundamental
 * @param {string|null} adminKey - Optional admin key for config endpoints
 * @returns {Promise<Object>} Theme policy config payload
 */
export const getThemePolicyConfig = async (pipeline = 'technical', adminKey = null) => {
  const headers = adminKey ? { 'X-Admin-Key': adminKey } : undefined;
  const response = await apiClient.get('/v1/config/theme-policies', {
    params: { pipeline },
    headers,
  });
  return response.data;
};

/**
 * Update theme policy with preview/stage/apply mode.
 *
 * @param {Object} payload - Theme policy update payload
 * @param {string|null} adminKey - Optional admin key for config endpoints
 * @param {string|null} adminActor - Optional actor label for audit trail
 * @returns {Promise<Object>} Update response with status/version/diff
 */
export const updateThemePolicy = async (payload, adminKey = null, adminActor = null) => {
  const headers = {};
  if (adminKey) headers['X-Admin-Key'] = adminKey;
  if (adminActor) headers['X-Admin-Actor'] = adminActor;
  const response = await apiClient.post('/v1/config/theme-policies', payload, {
    headers: Object.keys(headers).length ? headers : undefined,
  });
  return response.data;
};

/**
 * Promote staged theme policy to active.
 *
 * @param {string} pipeline - technical|fundamental
 * @param {string|null} note - Optional promotion note
 * @param {string|null} adminKey - Optional admin key
 * @param {string|null} adminActor - Optional actor label
 * @returns {Promise<Object>} Apply response
 */
export const promoteStagedThemePolicy = async (
  pipeline,
  note = null,
  adminKey = null,
  adminActor = null,
) => {
  const headers = {};
  if (adminKey) headers['X-Admin-Key'] = adminKey;
  if (adminActor) headers['X-Admin-Actor'] = adminActor;
  const response = await apiClient.post('/v1/config/theme-policies/promote-staged', null, {
    params: { pipeline, note },
    headers: Object.keys(headers).length ? headers : undefined,
  });
  return response.data;
};

/**
 * Revert to previous theme policy snapshot from history.
 *
 * @param {Object} payload - { pipeline, version_id, note? }
 * @param {string|null} adminKey - Optional admin key
 * @param {string|null} adminActor - Optional actor label
 * @returns {Promise<Object>} Apply response
 */
export const revertThemePolicy = async (payload, adminKey = null, adminActor = null) => {
  const headers = {};
  if (adminKey) headers['X-Admin-Key'] = adminKey;
  if (adminActor) headers['X-Admin-Actor'] = adminActor;
  const response = await apiClient.post('/v1/config/theme-policies/revert', payload, {
    headers: Object.keys(headers).length ? headers : undefined,
  });
  return response.data;
};

export const searchThemeEquivalence = async (pipeline, q = '') =>
  (await apiClient.get('/v1/themes/equivalence/search', { params: { pipeline, q } })).data;
export const previewThemeEquivalence = async (source_id, target_id) =>
  (await apiClient.get('/v1/themes/equivalence/preview', { params: { source_id, target_id } })).data;
export const assertSecureAdminTransport = ({
  production = import.meta.env.PROD,
  protocol = globalThis.location?.protocol,
  apiBaseUrl = import.meta.env.VITE_API_URL || '/api',
} = {}) => {
  if (!production) return;
  const absoluteProtocol = /^[a-z][a-z\d+.-]*:/i.exec(apiBaseUrl)?.[0]?.toLowerCase();
  if (absoluteProtocol === 'http:' || (!absoluteProtocol && protocol !== 'https:')) {
    throw new Error('Admin actions require HTTPS in production.');
  }
};
export const applyThemeEquivalence = async (body, adminKey, adminActor) => {
  assertSecureAdminTransport();
  return (await apiClient.post('/v1/themes/equivalence', body, {
    headers: { 'X-Admin-Key': adminKey, 'X-Admin-Actor': adminActor },
  })).data;
};
export const getThemeEquivalenceHistory = async (pipeline) =>
  (await apiClient.get('/v1/themes/equivalence/history', { params: { pipeline } })).data;
export const undoThemeEquivalence = async (id, body, adminKey, adminActor) => {
  assertSecureAdminTransport();
  return (await apiClient.post(`/v1/themes/equivalence/${id}/undo`, body, {
    headers: { 'X-Admin-Key': adminKey, 'X-Admin-Actor': adminActor },
  })).data;
};
export const getThemeDevelopments = async (id) =>
  (await apiClient.get(`/v1/themes/${id}/developments`)).data;
