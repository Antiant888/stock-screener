import { useMemo, memo } from 'react';
import { Box, Tooltip, Typography, useTheme } from '@mui/material';

import { monotoneXPath, scaleLinear } from './sparklineGeometry';

// The chart's own width is set by flex layout, so the SVG stretches a fixed
// viewBox horizontally: 1 unit = 1px vertically (margins and stroke stay in
// pixels) and the curve keeps its shape under the horizontal stretch.
const VIEW_WIDTH = 1000;
const MARGIN = 2;

/**
 * Price Sparkline Component
 *
 * Renders an area chart sparkline showing the price trend
 * over the last 30 trading days. Uses normalized prices for
 * consistent visual comparison across stocks.
 *
 * Color coding (theme success/error, so it follows light/dark mode):
 * - Green: Price up overall (30-day change positive)
 * - Red: Price down overall (30-day change negative)
 * - Grey: Flat
 *
 * Displays 1-day change as text badge
 */
function PriceSparkline({
  data,
  trend,
  change1d,
  industry,  // Industry to show in tooltip
  width = 100,
  height = 28,
  showChange = true,  // Whether to show the 1-day change text
  sparklineWidth = 60,  // Width of the inner sparkline chart when showChange is true
}) {
  const { palette } = useTheme();

  const viewHeight = typeof height === 'number' ? height : 28;

  // Transform data for chart
  const { chartData, line, area, color } = useMemo(() => {
    if (!data || !Array.isArray(data) || data.length === 0) {
      return { chartData: [], color: palette.text.disabled };
    }

    const chartData = data;

    const minVal = Math.min(...data);
    const maxVal = Math.max(...data);

    // Add 5% padding to domain
    const range = maxVal - minVal || 0.01;
    const padding = range * 0.05;

    // trend: 1 = up, -1 = down, 0 = flat
    const color = trend === 1 ? palette.success.main : trend === -1 ? palette.error.main : palette.text.disabled;

    const y = scaleLinear([minVal - padding, maxVal + padding], [viewHeight - MARGIN, MARGIN]);
    const step = data.length > 1 ? VIEW_WIDTH / (data.length - 1) : 0;
    const line = monotoneXPath(data.map((value, i) => [i * step, y(value)]));
    // Filled down to the padded minimum, like recharts' Area.
    const area = data.length > 1 ? `${line}L${VIEW_WIDTH},${viewHeight - MARGIN}L0,${viewHeight - MARGIN}Z` : '';

    return { chartData, line, area, color };
  }, [data, trend, palette, viewHeight]);

  // Format 1-day change for display
  const changeText = useMemo(() => {
    if (change1d === null || change1d === undefined) return null;
    const sign = change1d >= 0 ? '+' : '';
    return `${sign}${change1d.toFixed(1)}%`;
  }, [change1d]);

  const changeColor = useMemo(() => {
    if (change1d === null || change1d === undefined) return 'text.secondary';
    return change1d >= 0 ? 'success.main' : 'error.main';
  }, [change1d]);

  // Tooltip content
  const tooltipText = useMemo(() => {
    const parts = [];

    // Add industry if available
    if (industry) {
      parts.push(industry);
    }

    // Add 30-day trend description
    const trendText = trend === 1 ? 'Up' : trend === -1 ? 'Down' : 'Flat';
    if (data && data.length > 0) {
      const overallChange = ((data[data.length - 1] - data[0]) / data[0]) * 100;
      parts.push(`30d: ${overallChange >= 0 ? '+' : ''}${overallChange.toFixed(1)}% (${trendText})`);
    }

    return parts.join(' | ') || 'No data';
  }, [industry, trend, data]);

  // No data - show placeholder
  if (!chartData || chartData.length === 0) {
    return (
      <Box
        sx={{
          width,
          height,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: 'text.disabled',
          fontSize: 10,
        }}
      >
        -
      </Box>
    );
  }

  return (
    <Tooltip title={tooltipText} arrow placement="top">
      <Box
        sx={{
          width,
          height,
          cursor: 'pointer',
          display: 'flex',
          alignItems: 'center',
          gap: 0.5,
        }}
      >
        {/* Sparkline Chart */}
        <Box
          sx={{
            width: showChange ? sparklineWidth : '100%',
            height: '100%',
            flex: showChange ? '1 1 auto' : 1,
            minWidth: 0,
          }}
        >
          <svg
            width="100%"
            height="100%"
            viewBox={`0 0 ${VIEW_WIDTH} ${viewHeight}`}
            preserveAspectRatio="none"
            style={{ display: 'block' }}
          >
            <path d={area} fill={color} fillOpacity={0.25} />
            <path d={line} fill="none" stroke={color} strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
          </svg>
        </Box>

        {/* 1-Day Change Badge */}
        {showChange && changeText && (
          <Typography
            sx={{
              fontSize: 10,
              fontWeight: 600,
              fontFamily: 'monospace',
              color: changeColor,
              whiteSpace: 'nowrap',
              flexShrink: 0,
            }}
          >
            {changeText}
          </Typography>
        )}
      </Box>
    </Tooltip>
  );
}

// Memoize component - only re-render when data or key props change
export default memo(PriceSparkline, (prevProps, nextProps) => {
  // Deep compare data arrays
  if (prevProps.data === nextProps.data) {
    return (
      prevProps.trend === nextProps.trend &&
      prevProps.change1d === nextProps.change1d &&
      prevProps.width === nextProps.width &&
      prevProps.height === nextProps.height &&
      prevProps.sparklineWidth === nextProps.sparklineWidth
    );
  }

  if (!prevProps.data || !nextProps.data) return false;
  if (prevProps.data.length !== nextProps.data.length) return false;

  // For sparkline data, compare first, last, and length (sufficient for visual comparison)
  return (
    prevProps.data[0] === nextProps.data[0] &&
    prevProps.data[prevProps.data.length - 1] === nextProps.data[nextProps.data.length - 1] &&
    prevProps.trend === nextProps.trend &&
    prevProps.change1d === nextProps.change1d &&
    prevProps.width === nextProps.width &&
    prevProps.height === nextProps.height &&
    prevProps.sparklineWidth === nextProps.sparklineWidth
  );
});
