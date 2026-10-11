import { lazy, Suspense, useCallback, useContext, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import {
  AppBar,
  Box,
  Button,
  CircularProgress,
  Container,
  Drawer,
  Fab,
  InputBase,
  ListItemText,
  Menu,
  MenuItem,
  Toolbar,
  Typography,
  IconButton,
  useTheme,
} from '@mui/material';
import { Link as RouterLink, matchPath, useLocation, useNavigate } from 'react-router-dom';
import ShowChartIcon from '@mui/icons-material/ShowChart';
import Brightness4Icon from '@mui/icons-material/Brightness4';
import Brightness7Icon from '@mui/icons-material/Brightness7';
import SettingsIcon from '@mui/icons-material/Settings';
import PersonIcon from '@mui/icons-material/Person';
import SmartToyIcon from '@mui/icons-material/SmartToy';
import { ColorModeContext } from '../../contexts/ColorModeContext';
import MarketSelector from './MarketSelector';
import RuntimeActivityStatusButton from './RuntimeActivityStatusButton';
import { useRuntime } from '../../contexts/RuntimeContext';
import { useMarket, useMarketForCapability } from '../../contexts/MarketContext';
import { usePipeline } from '../../contexts/usePipeline';
import { useStrategyProfile, useStrategyProfileData } from '../../contexts/StrategyProfileContext';
import { isLiveOptionsAvailable } from '../../features/options/optionsAvailability';
import { loadStockDetails, loaderForPath } from '../../pageLoaders';
import {
  BREADTH_MARKET_FALLBACKS,
  GROUP_RANKING_MARKET_FALLBACKS,
  breadthBootstrapQuery,
  dailySnapshotQuery,
  groupsBootstrapQuery,
  scanBootstrapQuery,
} from '../../pageBootstrapQueries';

const AssistantDrawerContent = lazy(() => import('../AssistantChat/AssistantDrawerContent'));
const PipelineProgressCard = lazy(() => import('../PipelineProgressCard'));
const TaskSettingsModal = lazy(() => import('../Settings/TaskSettingsModal'));

function DrawerLoadingFallback() {
  return (
    <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%' }}>
      <CircularProgress size={24} />
    </Box>
  );
}

function PipelineProgressOverlay() {
  const { isCardVisible } = usePipeline();

  if (!isCardVisible) return null;

  return (
    <Suspense fallback={null}>
      <PipelineProgressCard />
    </Suspense>
  );
}

function StrategyProfileMenu({ anchorEl, onClose }) {
  const {
    activeProfile,
    activeProfileDetail,
    profiles,
    setActiveProfile,
  } = useStrategyProfileData();
  const profileOptions = profiles.length
    ? profiles
    : [{ profile: activeProfile, label: activeProfileDetail?.label || activeProfile }];

  return (
    <Menu
      anchorEl={anchorEl}
      open={Boolean(anchorEl)}
      onClose={onClose}
      anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
      transformOrigin={{ vertical: 'top', horizontal: 'right' }}
    >
      {profileOptions.map((profile) => (
        <MenuItem
          key={profile.profile}
          selected={profile.profile === activeProfile}
          onClick={() => {
            setActiveProfile(profile.profile);
            onClose();
          }}
        >
          <ListItemText primary={profile.label} />
        </MenuItem>
      ))}
    </Menu>
  );
}

function StrategyProfileControl() {
  const { activeProfile } = useStrategyProfile();
  const [profileMenuAnchor, setProfileMenuAnchor] = useState(null);
  const profileMenuOpen = Boolean(profileMenuAnchor);

  return (
    <>
      <IconButton
        sx={{ ml: 1 }}
        onClick={(event) => setProfileMenuAnchor(event.currentTarget)}
        color="inherit"
        title={`Profile: ${activeProfile}`}
        aria-label="Select strategy profile"
        aria-haspopup="menu"
        aria-expanded={profileMenuOpen}
        size="small"
      >
        <PersonIcon fontSize="small" />
      </IconButton>
      {profileMenuOpen && (
        <StrategyProfileMenu
          anchorEl={profileMenuAnchor}
          onClose={() => setProfileMenuAnchor(null)}
        />
      )}
    </>
  );
}

// The early fetch is the page's one attempt, as the route's own import would
// be: browsers keep a failed dynamic import for the session, so the route
// shows the same error it would have shown on main. Nothing to handle here.
const loadQuietly = (loader) => loader?.().catch(() => {});

/**
 * Start a page's code and first-paint data on hover, focus or touch, so a tab
 * switch does not wait for code, then data, after the click. The data only
 * goes where the page itself would look: same key, market and snapshot flag.
 * The nav renders once the capabilities request has settled, and with auth
 * required only after sign-in; the server checks every request regardless.
 */
function useNavPrefetch() {
  const queryClient = useQueryClient();
  const location = useLocation();
  const { uiSnapshots } = useRuntime();
  const { selectedMarket } = useMarket();
  const { market: groupsMarket } = useMarketForCapability(
    'group_rankings',
    GROUP_RANKING_MARKET_FALLBACKS,
  );
  const { market: breadthMarket } = useMarketForCapability('breadth', BREADTH_MARKET_FALLBACKS);

  return useCallback((path) => {
    loadQuietly(loaderForPath(path));
    // The open page owns its queries: the Scan page re-applies any new
    // latest-scan bootstrap, which would replace the scan the user picked.
    // matchPath follows the router (trailing slash, any casing).
    if (matchPath(path, location.pathname)) return;
    let query = null;
    if (path === '/') query = dailySnapshotQuery(selectedMarket);
    else if (path === '/scan' && uiSnapshots?.scan) query = scanBootstrapQuery(selectedMarket);
    else if (path === '/groups' && uiSnapshots?.groups) {
      query = groupsBootstrapQuery(queryClient, groupsMarket);
    } else if (path === '/breadth' && uiSnapshots?.breadth) {
      query = breadthBootstrapQuery(queryClient, breadthMarket);
    }
    if (!query) return;
    // A query without data is always stale, so a failed one would be asked
    // again on every hover; wait out its staleTime instead.
    const state = queryClient.getQueryState(query.queryKey);
    if (state?.status === 'error' && Date.now() - state.errorUpdatedAt < query.staleTime) return;
    queryClient.prefetchQuery(query);
  }, [breadthMarket, groupsMarket, location.pathname, queryClient, selectedMarket, uiSnapshots]);
}

function TickerSearch() {
  const navigate = useNavigate();
  const [tickerInput, setTickerInput] = useState('');

  const handleTickerSubmit = (event) => {
    if (event.key !== 'Enter') return;
    const ticker = tickerInput.trim().toUpperCase();
    if (!ticker) return;
    navigate(`/stocks/${encodeURIComponent(ticker)}`);
    setTickerInput('');
    event.target.blur();
  };

  return (
    <InputBase
      value={tickerInput}
      onChange={(e) => setTickerInput(e.target.value)}
      onKeyDown={handleTickerSubmit}
      onFocus={() => loadQuietly(loadStockDetails)}
      placeholder="TICKER"
      size="small"
      inputProps={{ 'aria-label': 'Go to ticker' }}
      sx={{
        px: 1.5,
        py: 0.25,
        width: 150,
        fontSize: '15px',
        backgroundColor: 'rgba(255,255,255,0.15)',
        borderRadius: 1,
        color: 'inherit',
        '& input::placeholder': {
          color: 'rgba(255,255,255,0.7)',
          opacity: 1,
          letterSpacing: '1px',
        },
      }}
    />
  );
}

function Layout({ children }) {
  const location = useLocation();
  const theme = useTheme();
  const colorMode = useContext(ColorModeContext);
  const { auth, features, isLoggingOut, logout, marketCatalog, primaryMarket } = useRuntime();
  const { selectedMarket } = useMarket();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [assistantOpen, setAssistantOpen] = useState(false);
  const prefetchPage = useNavPrefetch();

  const assistantAvailable = features.chatbot && (!auth?.required || auth?.authenticated);
  const optionsAvailable = isLiveOptionsAvailable({
    features,
    marketCatalog,
    selectedMarket,
    primaryMarket,
  });

  const navItems = [
    { path: '/', label: 'Daily' },
    { path: '/scan', label: 'Scan' },
    { path: '/breadth', label: 'Breadth' },
    { path: '/groups', label: 'Groups' },
    { path: '/validation', label: 'Backtest' },
    ...(optionsAvailable ? [{ path: '/options', label: 'Options', matchPrefix: true }] : []),
    ...(features.themes ? [{ path: '/themes', label: 'Themes' }] : []),
    ...(features.chatbot ? [{ path: '/chatbot', label: 'Assistant' }] : []),
  ];

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', minHeight: '100vh' }}>
      <AppBar position="static" sx={{ minHeight: 48 }}>
        <Toolbar variant="dense" sx={{ minHeight: 48, overflowX: 'auto', '& > *': { flexShrink: 0 } }}>
          <ShowChartIcon sx={{ mr: 1, fontSize: 20 }} />
          <Typography variant="subtitle1" component="div" sx={{ fontWeight: 600 }}>
            STOCK SCANNER
          </Typography>
          <RuntimeActivityStatusButton />
          <MarketSelector />
          <Box sx={{ flexGrow: 1 }} />
          <TickerSearch />
          <Box sx={{ flexGrow: 1 }} />
          {navItems.map((item) => {
            const isActive = item.matchPrefix
              ? location.pathname.startsWith(item.path)
              : location.pathname === item.path;
            return (
              <Button
                key={item.path}
                color="inherit"
                component={RouterLink}
                to={item.path}
                onMouseEnter={() => prefetchPage(item.path)}
                onFocus={() => prefetchPage(item.path)}
                onTouchStart={() => prefetchPage(item.path)}
                size="small"
                sx={{
                  backgroundColor: isActive ? 'rgba(255, 255, 255, 0.15)' : 'transparent',
                  borderBottom: isActive ? '2px solid white' : '2px solid transparent',
                  borderRadius: 0,
                  fontWeight: isActive ? 600 : 400,
                  fontSize: '12px',
                  px: 1.5,
                  py: 0.5,
                  '&:hover': {
                    backgroundColor: 'rgba(255, 255, 255, 0.25)',
                  },
                }}
              >
                {item.label}
              </Button>
            );
          })}
          <StrategyProfileControl />
          {features.tasks && (
            <IconButton
              sx={{ ml: 0.5 }}
              onClick={() => setSettingsOpen(true)}
              color="inherit"
              title="Scheduled Tasks"
              size="small"
            >
              <SettingsIcon fontSize="small" />
            </IconButton>
          )}
          {auth?.required && auth?.authenticated && (
            <Button
              color="inherit"
              size="small"
              onClick={() => logout()}
              disabled={isLoggingOut}
              sx={{ ml: 1, fontSize: '12px' }}
            >
              Sign out
            </Button>
          )}
          <IconButton
            sx={{ ml: 0.5 }}
            onClick={colorMode.toggleColorMode}
            color="inherit"
            title={theme.palette.mode === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
            size="small"
          >
            {theme.palette.mode === 'dark' ? <Brightness7Icon fontSize="small" /> : <Brightness4Icon fontSize="small" />}
          </IconButton>
        </Toolbar>
      </AppBar>

      <Container maxWidth="xl" sx={{ mt: 1, mb: 1, flex: 1 }}>
        {children}
      </Container>

      {features.themes && <PipelineProgressOverlay />}

      {features.tasks && settingsOpen && (
        <Suspense fallback={null}>
          <TaskSettingsModal open={settingsOpen} onClose={() => setSettingsOpen(false)} />
        </Suspense>
      )}

      {assistantAvailable && location.pathname !== '/chatbot' && (
        <>
          <Fab
            color="secondary"
            aria-label="Open assistant"
            onClick={() => setAssistantOpen(true)}
            sx={{
              position: 'fixed',
              right: 24,
              bottom: 24,
              zIndex: theme.zIndex.drawer - 1,
            }}
          >
            <SmartToyIcon />
          </Fab>

          <Drawer
            anchor="right"
            open={assistantOpen}
            onClose={() => setAssistantOpen(false)}
            PaperProps={{
              sx: {
                width: { xs: '100%', sm: 440 },
              },
            }}
          >
            {assistantOpen && (
              <Suspense fallback={<DrawerLoadingFallback />}>
                <AssistantDrawerContent onClose={() => setAssistantOpen(false)} />
              </Suspense>
            )}
          </Drawer>
        </>
      )}
    </Box>
  );
}

export default Layout;
