import { useMemo } from 'react';
import { alpha, useTheme } from '@mui/material/styles';

/**
 * Shared RRG quadrant colors — single source of truth for the live and static
 * charts (and any consumer that colors by quadrant, e.g. a detail modal). Each
 * quadrant maps to a theme tone so the colours follow light/dark mode.
 */
export const QUADRANT_TONES = {
  Leading: 'success',
  Weakening: 'warning',
  Lagging: 'error',
  Improving: 'primary',
};

export function useQuadrantColors() {
  const { palette } = useTheme();
  return useMemo(() => {
    const colors = Object.fromEntries(
      Object.entries(QUADRANT_TONES).map(([quadrant, tone]) => [quadrant, palette[tone].main]),
    );
    const fills = Object.fromEntries(
      Object.entries(colors).map(([quadrant, color]) => [quadrant, alpha(color, 0.08)]),
    );
    const colorOf = (quadrant) => colors[quadrant] || palette.text.disabled;
    return { colors, fills, colorOf };
  }, [palette]);
}

/**
 * Hover-highlight colors. A dark outline under a silver core reads on both
 * themes: the outline gives the silver an edge on the white paper, while the
 * silver core already contrasts on the near-black dark paper — so no theme lookup.
 */
export const HIGHLIGHT = '#C9CCD1'; // silver core
export const HIGHLIGHT_HALO = 'rgba(0,0,0,0.55)'; // outline beneath the silver

/**
 * Canonical quadrant order (clockwise rotation cycle) — single source of truth
 * for the quadrant filter buttons so they stay consistent with the colors above.
 */
export const QUADRANTS = ['Improving', 'Leading', 'Weakening', 'Lagging'];

/**
 * Per-quadrant plot geometry: which half of each axis the quadrant occupies
 * relative to the 100/100 cross ('lo' below, 'hi' above) and where its
 * backdrop label sits. Drives the RRG quadrant backdrops.
 */
export const QUADRANT_LAYOUT = [
  { name: 'Leading', x: 'hi', y: 'hi', labelPosition: 'insideTopRight' },
  { name: 'Weakening', x: 'hi', y: 'lo', labelPosition: 'insideBottomRight' },
  { name: 'Lagging', x: 'lo', y: 'lo', labelPosition: 'insideBottomLeft' },
  { name: 'Improving', x: 'lo', y: 'hi', labelPosition: 'insideTopLeft' },
];
