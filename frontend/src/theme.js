import { alpha } from '@mui/material/styles';

export const COLOR_MODE_STORAGE_KEY = 'stockscanner-color-mode';

// Saved choice first, then the OS preference; the app has always opened dark.
// index.html runs the same lookup inline so the first paint matches.
export function readColorMode() {
  try {
    const saved = window.localStorage.getItem(COLOR_MODE_STORAGE_KEY);
    if (saved === 'light' || saved === 'dark') return saved;
  } catch {
    // Storage can be blocked (private mode, sandboxed iframes).
  }
  return window.matchMedia?.('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
}

export function saveColorMode(mode) {
  try {
    window.localStorage.setItem(COLOR_MODE_STORAGE_KEY, mode);
  } catch {
    // Not persisting is fine; the toggle still works for this session.
  }
}

// One accent (primary) for selection and focus. success/error are reserved for
// gains and losses. index.css repeats background.default for the pre-React paint.
const PALETTES = {
  dark: {
    primary: { main: '#7b93ff', contrastText: '#0b0d12' },
    secondary: { main: '#b197fc', contrastText: '#0b0d12' },
    success: { main: '#2ebd85' },
    error: { main: '#f6465d' },
    background: { default: '#0b0d12', paper: '#12151c', subtle: '#181c25' },
    divider: '#242a37',
    text: { primary: '#e7e9ee', secondary: '#8b93a7', disabled: '#5b6378' },
  },
  light: {
    primary: { main: '#3b5bdb', contrastText: '#ffffff' },
    secondary: { main: '#7048e8', contrastText: '#ffffff' },
    success: { main: '#0e8a5f' },
    error: { main: '#d92d3a' },
    background: { default: '#f5f6f8', paper: '#ffffff', subtle: '#f2f4f7' },
    divider: '#e3e6ec',
    text: { primary: '#111827', secondary: '#5f6b7f', disabled: '#98a2b3' },
  },
};

export const getDesignTokens = (mode) => {
  const palette = PALETTES[mode];
  return {
    palette: { mode, ...palette },
    shape: {
      borderRadius: 10,
    },
    typography: {
      fontFamily: '"Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
      fontSize: 13,
      body1: {
        fontSize: '14px',
      },
      body2: {
        fontSize: '13px',
        lineHeight: 1.5,
      },
      caption: {
        fontSize: '11px',
      },
      h6: {
        fontSize: '15px',
        fontWeight: 600,
      },
      button: {
        textTransform: 'none',
        fontWeight: 600,
      },
    },
    components: {
      MuiCssBaseline: {
        styleOverrides: {
          // Columns of prices and scores line up without a monospace font.
          body: { fontVariantNumeric: 'tabular-nums' },
        },
      },
      MuiPaper: {
        styleOverrides: {
          // Flat surfaces: no dark-mode elevation tint.
          root: { backgroundImage: 'none' },
        },
      },
      MuiAppBar: {
        defaultProps: {
          elevation: 0,
          color: 'inherit',
        },
        styleOverrides: {
          root: {
            backgroundColor: palette.background.paper,
            borderBottom: `1px solid ${palette.divider}`,
          },
        },
      },
      MuiTableCell: {
        styleOverrides: {
          root: {
            padding: '4px 6px',
            fontSize: '11px',
            lineHeight: 1.3,
            borderBottom: `1px solid ${palette.divider}`,
          },
          head: {
            backgroundColor: palette.background.subtle,
            color: palette.text.secondary,
            fontWeight: 600,
            fontSize: '11px',
            textTransform: 'uppercase',
            letterSpacing: '0.04em',
            padding: '6px 6px',
            whiteSpace: 'nowrap',
          },
          sizeSmall: {
            padding: '3px 5px',
          },
        },
      },
      MuiTableRow: {
        styleOverrides: {
          root: {
            height: 24,
            '&.MuiTableRow-head': {
              height: 28,
            },
          },
        },
      },
      MuiTableBody: {
        styleOverrides: {
          root: {
            // :where() keeps the stripe at the lowest specificity, so a row's own
            // background (selected or highlighted rows) still wins on odd rows.
            '& :where(.MuiTableRow-root:nth-of-type(odd))': {
              backgroundColor: alpha(palette.text.primary, 0.025),
            },
            '& .MuiTableRow-root:hover': {
              backgroundColor: alpha(palette.primary.main, 0.08),
            },
          },
        },
      },
      MuiTableSortLabel: {
        styleOverrides: {
          root: {
            '&:hover, &.Mui-active': {
              color: palette.primary.main,
            },
            '&.Mui-active .MuiTableSortLabel-icon': {
              color: palette.primary.main,
            },
          },
        },
      },
      MuiChip: {
        styleOverrides: {
          sizeSmall: {
            height: 20,
            fontSize: '11px',
          },
          labelSmall: {
            padding: '0 6px',
          },
        },
      },
      MuiIconButton: {
        styleOverrides: {
          sizeSmall: {
            padding: 2,
          },
        },
      },
      MuiCardContent: {
        styleOverrides: {
          root: {
            padding: 12,
            '&:last-child': {
              paddingBottom: 12,
            },
          },
        },
      },
      MuiTab: {
        styleOverrides: {
          root: {
            minHeight: 40,
            fontSize: '12px',
          },
        },
      },
      MuiTabs: {
        styleOverrides: {
          root: {
            minHeight: 40,
          },
        },
      },
    },
  };
};
