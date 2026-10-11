import { useMediaQuery } from '@mui/material';
import TodayIcon from '@mui/icons-material/Today';
import FilterListIcon from '@mui/icons-material/FilterList';
import StackedLineChartIcon from '@mui/icons-material/StackedLineChart';
import LeaderboardIcon from '@mui/icons-material/Leaderboard';

// Bottom bar height on phones; fixed overlays and page padding clear it.
export const MOBILE_NAV_HEIGHT = 56;
export const mobileNavOffset = (gap = 0) => `calc(${MOBILE_NAV_HEIGHT + gap}px + env(safe-area-inset-bottom))`;

// Phones and portrait tablets get the bottom bar; the desktop top-bar nav needs ~900px.
// Toolbar/Container gutters that also clear a landscape notch (viewport-fit=cover).
export const safeGutters = {
  pl: { xs: 'max(16px, env(safe-area-inset-left))', sm: 'max(24px, env(safe-area-inset-left))' },
  pr: { xs: 'max(16px, env(safe-area-inset-right))', sm: 'max(24px, env(safe-area-inset-right))' },
};

export const useIsMobileShell = () => useMediaQuery((theme) => theme.breakpoints.down('md'));

export const CORE_NAV_ITEMS = [
  { path: '/', label: 'Daily', icon: <TodayIcon /> },
  { path: '/scan', label: 'Scan', icon: <FilterListIcon /> },
  { path: '/breadth', label: 'Breadth', icon: <StackedLineChartIcon /> },
  { path: '/groups', label: 'Groups', icon: <LeaderboardIcon /> },
];
