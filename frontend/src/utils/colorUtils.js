/**
 * Centralized color utility functions for consistent styling across components
 *
 * These functions return MUI palette keys ('success.main', ...) for stock
 * metrics like stage, ratings, growth percentages, and industry group rankings,
 * so colours follow the active light/dark theme. Use them in `sx`; for a badge
 * fill, spread onPalette(key) to get readable text as well.
 */

/**
 * Get color for Minervini stock stage (1-4)
 * @param {number} stage - Stage number (1-4)
 * @returns {string} MUI palette key
 */
export const getStageColor = (stage) => {
  switch (stage) {
    case 2:
      return 'success.main'; // Advancing
    case 3:
      return 'warning.main'; // Topping
    case 4:
      return 'error.main'; // Declining
    default:
      return 'grey.500'; // Basing / unknown
  }
};

/**
 * Get MUI color name for stock rating (Strong Buy, Buy, Watch, Hold)
 * @param {string} rating - Rating string
 * @returns {string} MUI color name (success, warning, default)
 */
export const getRatingColor = (rating) => {
  switch (rating) {
    case 'Strong Buy':
    case 'Buy':
      return 'success';
    case 'Watch':
      return 'warning';
    default:
      return 'default';
  }
};

/**
 * Get color for growth percentage values. Any positive growth is success.main:
 * a lighter green can't reach 4.5:1 as small text on the light surfaces.
 * @param {number|null} value - Growth percentage
 * @returns {string} MUI palette key
 */
export const getGrowthColor = (value) => {
  if (value == null) return 'text.secondary';
  if (value >= 0) return 'success.main';
  if (value >= -10) return 'warning.main';
  return 'error.main';
};

/**
 * Get color for EPS Rating (0-99 scale, IBD-style)
 * @param {number|null} value - EPS rating value
 * @returns {string} MUI color path
 */
export const getEpsRatingColor = (value) => {
  if (value == null) return 'text.secondary';
  if (value >= 80) return 'success.main';
  if (value >= 50) return 'text.primary';
  return 'error.main';
};

/**
 * Get color for IBD industry group rank (1-197)
 * @param {number|null} rank - Group rank
 * @returns {string} MUI color path
 */
export const getGroupRankColor = (rank) => {
  if (rank == null) return 'text.secondary';
  if (rank <= 20) return 'success.main';   // Top ~10%
  if (rank >= 177) return 'error.main';    // Bottom ~10%
  return 'warning.main';                    // Middle
};

/**
 * Background plus readable text for a theme palette key used as a badge fill.
 * '<tone>.main' uses that tone's contrastText; light greys get dark text; any
 * other key (e.g. 'text.secondary') gets the inverse surface colour. A missing
 * key (e.g. getScoreColor(null)) falls back to a neutral grey.
 * @param {string|null|undefined} key - Palette key such as 'success.main' or 'grey.400'
 * @returns {{bgcolor: string, color: string}} sx props
 */
export const onPalette = (key) => {
  const fill = key || 'grey.500';
  return {
    bgcolor: fill,
    color: fill.endsWith('.main')
      ? fill.replace(/\.main$/, '.contrastText')
      : fill.startsWith('grey.') ? 'grey.900' : 'background.paper',
  };
};
