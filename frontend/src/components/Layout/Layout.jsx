import { lazy, Suspense, useCallback, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import {
  AppBar,
  Box,
  Button,
  CircularProgress,
  Container,
  Divider,
  Drawer,
  Fab,
  InputBase,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  ListSubheader,
  Menu,
  MenuItem,
  Toolbar,
  IconButton,
  useTheme,
} from '@mui/material';
import { matchPath, useLocation, useNavigate } from 'react-router-dom';
import CandlestickChartIcon from '@mui/icons-material/CandlestickChart';
import AutoAwesomeIcon from '@mui/icons-material/AutoAwesome';
import CheckIcon from '@mui/icons-material/Check';
import HistoryIcon from '@mui/icons-material/History';
import LogoutIcon from '@mui/icons-material/Logout';
import PersonIcon from '@mui/icons-material/Person';
import SmartToyIcon from '@mui/icons-material/SmartToy';
import MarketSelector from './MarketSelector';
import RuntimeActivityStatusButton from './RuntimeActivityStatusButton';
import {
  BrandMark,
  ColorModeButton,
  ColorModeListItem,
  DesktopNav,
  MobileBottomNav,
} from './AppNav';
import { CORE_NAV_ITEMS, mobileNavOffset, safeGutters, useIsMobileShell } from './shellNav';
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

function useProfileOptions() {
  const {
    activeProfile,
    activeProfileDetail,
    profiles,
    setActiveProfile,
  } = useStrategyProfileData();
  const profileOptions = profiles.length
    ? profiles
    : [{ profile: activeProfile, label: activeProfileDetail?.label || activeProfile }];
  return { activeProfile, profileOptions, setActiveProfile };
}

function StrategyProfileMenu({ anchorEl, onClose }) {
  const { activeProfile, profileOptions, setActiveProfile } = useProfileOptions();

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

// Mounted only while the "More" sheet is open, so profiles load on demand like the desktop menu.
function MobileProfileItems({ onDone }) {
  const { activeProfile, profileOptions, setActiveProfile } = useProfileOptions();

  return (
    <>
      <ListSubheader disableSticky sx={{ lineHeight: '32px', bgcolor: 'transparent' }}>Strategy profile</ListSubheader>
      {profileOptions.map((profile) => (
        <ListItemButton
          key={profile.profile}
          selected={profile.profile === activeProfile}
          onClick={() => {
            setActiveProfile(profile.profile);
            onDone();
          }}
        >
          <ListItemIcon>{profile.profile === activeProfile ? <CheckIcon /> : <PersonIcon />}</ListItemIcon>
          <ListItemText primary={profile.label} />
        </ListItemButton>
      ))}
    </>
  );
}

function StrategyProfileControl() {
  const { activeProfile } = useStrategyProfile();
  const [profileMenuAnchor, setProfileMenuAnchor] = useState(null);
  const profileMenuOpen = Boolean(profileMenuAnchor);

  return (
    <>
      <IconButton
        onClick={(event) => setProfileMenuAnchor(event.currentTarget)}
        title={`Profile: ${activeProfile}`}
        aria-label="Select strategy profile"
        aria-haspopup="menu"
        aria-expanded={profileMenuOpen}
        size="small"
        sx={{ color: 'text.secondary' }}
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
      placeholder="Ticker"
      size="small"
      inputProps={{ 'aria-label': 'Go to ticker', enterKeyHint: 'go', autoCapitalize: 'characters' }}
      sx={{
        px: 1.25,
        height: 32,
        width: { xs: 88, md: 160 },
        // 16px on phones: iOS Safari zooms into inputs below that.
        fontSize: { xs: '16px', md: '14px' },
        backgroundColor: 'action.hover',
        border: 1,
        borderColor: 'divider',
        borderRadius: 2,
        '&.Mui-focused': { borderColor: 'primary.main' },
        '& input::placeholder': {
          color: 'text.secondary',
          opacity: 1,
        },
      }}
    />
  );
}

function Layout({ children }) {
  const location = useLocation();
  const theme = useTheme();
  const isMobile = useIsMobileShell();
  const { auth, features, isLoggingOut, logout, marketCatalog, primaryMarket } = useRuntime();
  const { selectedMarket } = useMarket();
  const [assistantOpen, setAssistantOpen] = useState(false);
  const prefetchPage = useNavPrefetch();

  const assistantAvailable = features.chatbot && (!auth?.required || auth?.authenticated);
  const canSignOut = auth?.required && auth?.authenticated;
  const optionsAvailable = isLiveOptionsAvailable({
    features,
    marketCatalog,
    selectedMarket,
    primaryMarket,
  });

  const navItems = [
    ...CORE_NAV_ITEMS,
    { path: '/validation', label: 'Backtest', icon: <HistoryIcon /> },
    ...(optionsAvailable ? [{ path: '/options', label: 'Options', matchPrefix: true, icon: <CandlestickChartIcon /> }] : []),
    ...(features.themes ? [{ path: '/themes', label: 'Themes', icon: <AutoAwesomeIcon /> }] : []),
    ...(features.chatbot ? [{ path: '/chatbot', label: 'Assistant', icon: <SmartToyIcon /> }] : []),
  ];

  const mobileExtras = (close) => (
    <>
      <MobileProfileItems onDone={close} />
      <Divider sx={{ my: 0.5 }} />
      <ColorModeListItem onDone={close} />
      {canSignOut && (
        <ListItemButton disabled={isLoggingOut} onClick={() => { close(); logout(); }}>
          <ListItemIcon><LogoutIcon /></ListItemIcon>
          <ListItemText primary="Sign out" />
        </ListItemButton>
      )}
    </>
  );

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', minHeight: '100vh' }}>
      <AppBar position="static">
        <Toolbar
          variant="dense"
          sx={{ ...safeGutters, minHeight: 52, gap: { xs: 1, md: 1.5 }, overflowX: 'auto', '& > *': { flexShrink: 0 } }}
        >
          <BrandMark label={isMobile ? null : 'Stock Scanner'} />
          <RuntimeActivityStatusButton compact={isMobile} />
          <MarketSelector />
          <Box sx={{ flexGrow: 1 }} />
          <TickerSearch />
          {!isMobile && (
            <>
              <Box sx={{ flexGrow: 1 }} />
              <DesktopNav items={navItems} onIntent={prefetchPage} />
              <StrategyProfileControl />
              {canSignOut && (
                <Button
                  size="small"
                  onClick={() => logout()}
                  disabled={isLoggingOut}
                  sx={{ color: 'text.secondary', fontSize: '12px' }}
                >
                  Sign out
                </Button>
              )}
              <ColorModeButton />
            </>
          )}
        </Toolbar>
      </AppBar>

      <Container maxWidth="xl" sx={{ ...safeGutters, mt: 1, mb: 1, flex: 1, pb: { xs: mobileNavOffset(8), md: 0 } }}>
        {children}
      </Container>

      {isMobile && <MobileBottomNav items={navItems} extras={mobileExtras} onIntent={prefetchPage} />}

      {features.themes && <PipelineProgressOverlay />}

      {assistantAvailable && location.pathname !== '/chatbot' && (
        <>
          <Fab
            color="secondary"
            aria-label="Open assistant"
            onClick={() => setAssistantOpen(true)}
            size={isMobile ? 'medium' : 'large'}
            sx={{
              position: 'fixed',
              right: { xs: 16, md: 24 },
              bottom: { xs: mobileNavOffset(16), md: 24 },
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
