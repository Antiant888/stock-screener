import { alpha } from '@mui/material/styles';

export const groupRsTone = (value) => {
  if (!Number.isFinite(value)) return 'neutral';
  const displayedValue = Number(value.toFixed(1));
  if (displayedValue >= 80) return 'up-strong';
  if (displayedValue >= 70) return 'up-soft';
  if (displayedValue <= 20) return 'down-strong';
  if (displayedValue <= 30) return 'down-soft';
  return 'neutral';
};

// Same visual language as the Scan score tints (translucent theme
// success/error, normal text), on the RS bands above so the table, the group
// matrix and its legend, and data-rs-tone all agree.
const RS_TINTS = {
  'up-strong': ['success', 0.42],
  'up-soft': ['success', 0.2],
  'down-soft': ['error', 0.2],
  'down-strong': ['error', 0.34],
};

export const groupRsCellSx = (value) => {
  const tint = RS_TINTS[groupRsTone(value)];
  if (!tint) return {};
  const [tone, opacity] = tint;
  return { backgroundColor: (theme) => alpha(theme.palette[tone].main, opacity) };
};
