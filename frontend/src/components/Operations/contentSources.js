import { useQuery } from '@tanstack/react-query';
import { getContentSources } from '../../api/themes';

const REFRESH_MS = 60000;

// Same key and args as ManageSourcesModal so both read one cache entry.
export function useContentSources({ enabled = true } = {}) {
  return useQuery({
    queryKey: ['contentSources'],
    queryFn: () => getContentSources(false),
    refetchInterval: REFRESH_MS,
    enabled,
  });
}

// Social-managed sources are reported by the social signal panel instead.
export function activeSources(sources) {
  return (sources ?? []).filter((source) => source.is_active && !source.social_managed);
}

export function failingSources(sources) {
  return activeSources(sources)
    .filter((source) => source.consecutive_failures > 0)
    .sort((a, b) => b.consecutive_failures - a.consecutive_failures);
}
