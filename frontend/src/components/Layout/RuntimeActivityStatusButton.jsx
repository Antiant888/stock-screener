import { useEffect, useState } from 'react';
import { Box, Button, Chip, Typography } from '@mui/material';
import { Link as RouterLink } from 'react-router-dom';
import { useRuntimeActivity } from '../../hooks/useRuntimeActivity';

// Header status is best-effort chrome; let route-critical data requests start first.
const HEADER_ACTIVITY_DELAY_MS = 1500;
const WARNING_MARKET_STATUSES = new Set(['failed', 'stale', 'stuck']);

function buildSummary(activity, { isPending = false, isError = false } = {}) {
  if (isError && isPending) {
    return {
      badge: 'Warn',
      badgeColor: 'warning',
      title: 'Activity unavailable',
      detail: 'View operations',
    };
  }

  if (isPending) {
    return {
      badge: '...',
      badgeColor: 'default',
      title: 'Checking activity',
      detail: 'View operations',
    };
  }

  const bootstrap = activity?.bootstrap ?? {};
  const summary = activity?.summary ?? {};
  const markets = activity?.markets ?? [];
  const activeMarket = markets.find((market) => (
    market.status === 'running' || market.status === 'queued'
  ));
  const warningMarket = markets.find((market) => (
    WARNING_MARKET_STATUSES.has(market.status)
  ));

  if (bootstrap.state === 'running') {
    const determinateBootstrap = (
      bootstrap.progress_mode === 'determinate'
      && Number.isFinite(bootstrap.percent)
    );
    const percent = determinateBootstrap ? Math.round(bootstrap.percent) : null;
    return {
      badge: determinateBootstrap ? `${percent}%` : 'Sync',
      badgeColor: 'info',
      title: `Bootstrapping ${bootstrap.primary_market || 'market'}`,
      detail: determinateBootstrap && bootstrap.current_stage
        ? `${bootstrap.current_stage} · ${percent}%`
        : bootstrap.current_stage
          || bootstrap.message
          || 'Preparing market data',
    };
  }

  if (summary.status === 'warning' && warningMarket) {
    const warningFallback = warningMarket.status === 'failed' ? 'Task failed' : 'Task needs attention';
    return {
      badge: 'Warn',
      badgeColor: 'warning',
      title: 'Refresh warning',
      detail: `${warningMarket.market} · ${warningMarket.stage_label || warningMarket.message || warningFallback}`,
    };
  }

  if ((summary.active_market_count ?? 0) > 0) {
    const activeLabel = summary.active_market_count === 1 ? '1 market active' : `${summary.active_market_count} markets active`;
    return {
      badge: String(summary.active_market_count),
      badgeColor: 'secondary',
      title: activeLabel,
      detail: activeMarket
        ? `${activeMarket.market} · ${activeMarket.stage_label || activeMarket.message || activeMarket.status}`
        : 'Data refresh in progress',
    };
  }

  return {
    badge: 'OK',
    badgeColor: 'success',
    title: 'Markets ready',
    detail: 'View operations',
  };
}

export default function RuntimeActivityStatusButton({ compact = false }) {
  const [activityEnabled, setActivityEnabled] = useState(false);
  useEffect(() => {
    const timerId = window.setTimeout(() => {
      setActivityEnabled(true);
    }, HEADER_ACTIVITY_DELAY_MS);

    return () => window.clearTimeout(timerId);
  }, []);

  const activityQuery = useRuntimeActivity({ enabled: activityEnabled });
  const summary = buildSummary(activityQuery.data, {
    isError: activityQuery.isError,
    isPending: !activityQuery.dataUpdatedAt,
  });

  return (
    <Button
      color="inherit"
      component={RouterLink}
      to="/operations"
      // Compact mode shows only the badge, so the title and detail move to the label.
      aria-label={compact ? `${summary.title}: ${summary.detail}` : undefined}
      title={compact ? `${summary.title} · ${summary.detail}` : undefined}
      sx={{
        minWidth: 0,
        px: compact ? 0.75 : 1,
        py: 0.25,
        borderRadius: 1.5,
        textTransform: 'none',
        display: 'flex',
        alignItems: 'center',
        gap: 1,
        maxWidth: 280,
        border: 1,
        borderColor: 'divider',
        backgroundColor: 'action.hover',
        '&:hover': {
          backgroundColor: 'action.selected',
        },
      }}
    >
      <Chip
        label={summary.badge}
        size="small"
        color={summary.badgeColor}
        sx={{
          height: 18,
          fontSize: '11px',
          fontWeight: 700,
          '& .MuiChip-label': {
            px: 0.75,
          },
        }}
      />
      {!compact && (
        <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', overflow: 'hidden' }}>
          <Typography
            variant="caption"
            sx={{
              color: 'inherit',
              fontWeight: 700,
              lineHeight: 1.2,
              whiteSpace: 'nowrap',
              textOverflow: 'ellipsis',
              overflow: 'hidden',
              maxWidth: 220,
            }}
          >
            {summary.title}
          </Typography>
          <Typography
            variant="caption"
            sx={{
              color: 'text.secondary',
              lineHeight: 1.2,
              whiteSpace: 'nowrap',
              textOverflow: 'ellipsis',
              overflow: 'hidden',
              maxWidth: 220,
            }}
          >
            {summary.detail}
          </Typography>
        </Box>
      )}
    </Button>
  );
}
