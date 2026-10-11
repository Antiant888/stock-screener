import { alpha } from '@mui/material/styles';

/**
 * Background tint for 0–100 scores (screener scores, RS ratings) so a column
 * can be scanned without reading every number. Neutral between 40 and 60;
 * green deepens toward 100 and red toward 0, in a few steps so the table can
 * use precomputed classes instead of per-cell styles.
 *
 * Buckets use the displayed value (one decimal), so the tint always agrees
 * with the number. A missing value is neutral; callers whose 0 means "no
 * value" (screener scores) pass null for it.
 */
export const HEAT_UP_STEPS = 5;
export const HEAT_DOWN_STEPS = 3;

export const heatStep = (value) => {
  if (value == null || !Number.isFinite(value)) return null;
  const shown = Number(value.toFixed(1));
  if (shown >= 60) {
    return { dir: 'up', step: Math.min(HEAT_UP_STEPS, 1 + Math.floor((shown - 60) / (40 / HEAT_UP_STEPS))) };
  }
  if (shown <= 40) {
    return { dir: 'down', step: Math.min(HEAT_DOWN_STEPS, 1 + Math.floor((40 - shown) / (40 / HEAT_DOWN_STEPS))) };
  }
  return null;
};

export const heatFill = (palette, { dir, step }) => (
  dir === 'up'
    ? alpha(palette.success.main, 0.08 + (0.42 * step) / HEAT_UP_STEPS)
    : alpha(palette.error.main, 0.08 + (0.26 * step) / HEAT_DOWN_STEPS)
);

/** Class name for tables that precompute the steps (see heatClassStyles). */
export const heatClass = (value) => {
  const heat = heatStep(value);
  return heat ? `rt-heat-${heat.dir}-${heat.step}` : '';
};

/** `& .rt-heat-*` rules for a styled() parent, generated from the theme. */
export const heatClassStyles = (palette) => Object.fromEntries([
  ...Array.from({ length: HEAT_UP_STEPS }, (_, i) => ({ dir: 'up', step: i + 1 })),
  ...Array.from({ length: HEAT_DOWN_STEPS }, (_, i) => ({ dir: 'down', step: i + 1 })),
].map((heat) => [`& .rt-heat-${heat.dir}-${heat.step}`, { backgroundColor: heatFill(palette, heat) }]));

/** sx for a single cell. */
export const heatSx = (value) => {
  const heat = heatStep(value);
  return heat ? { backgroundColor: (theme) => heatFill(theme.palette, heat) } : {};
};
