import { alpha } from '@mui/material/styles';

/**
 * Background tint for 0–100 screener scores so a column can be scanned
 * without reading every number. Neutral between 40 and 60; green deepens
 * toward 100 and red toward 0, in a few steps so the table can use
 * precomputed classes instead of per-cell styles.
 *
 * Buckets use the displayed value (one decimal), so the tint always agrees
 * with the number. A missing value is neutral; callers whose 0 means "no
 * value" (screener scores) pass null for it.
 */
// Lower edge of each step, whole numbers so neighbouring displayed values
// (e.g. 26.6 vs 26.7) never split oddly.
const UP_EDGES = [60, 68, 76, 84, 92];
const DOWN_EDGES = [40, 27, 14]; // at or below

export const heatStep = (value) => {
  if (value == null || !Number.isFinite(value)) return null;
  const shown = Number(value.toFixed(1));
  if (shown >= UP_EDGES[0]) {
    return { dir: 'up', step: UP_EDGES.filter((edge) => shown >= edge).length };
  }
  if (shown <= DOWN_EDGES[0]) {
    return { dir: 'down', step: DOWN_EDGES.filter((edge) => shown <= edge).length };
  }
  return null;
};

export const heatFill = (palette, { dir, step }) => (
  dir === 'up'
    ? alpha(palette.success.main, 0.08 + (0.42 * step) / UP_EDGES.length)
    : alpha(palette.error.main, 0.08 + (0.26 * step) / DOWN_EDGES.length)
);

/** Class name for tables that precompute the steps (see heatClassStyles). */
export const heatClass = (value) => {
  const heat = heatStep(value);
  return heat ? `rt-heat-${heat.dir}-${heat.step}` : '';
};

/** `& .rt-heat-*` rules for a styled() parent, generated from the theme. */
export const heatClassStyles = (palette) => Object.fromEntries([
  ...UP_EDGES.map((_, i) => ({ dir: 'up', step: i + 1 })),
  ...DOWN_EDGES.map((_, i) => ({ dir: 'down', step: i + 1 })),
].map((heat) => [`& .rt-heat-${heat.dir}-${heat.step}`, { backgroundColor: heatFill(palette, heat) }]));
