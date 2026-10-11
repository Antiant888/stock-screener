import { useContext, useState } from 'react';
import {
  BottomNavigation,
  BottomNavigationAction,
  Box,
  Button,
  Divider,
  Drawer,
  IconButton,
  List,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  Paper,
  Typography,
} from '@mui/material';
import { alpha } from '@mui/material/styles';
import { Link as RouterLink, useLocation } from 'react-router-dom';
import ShowChartIcon from '@mui/icons-material/ShowChart';
import MoreHorizIcon from '@mui/icons-material/MoreHoriz';
import DarkModeIcon from '@mui/icons-material/DarkMode';
import LightModeIcon from '@mui/icons-material/LightMode';
import { ColorModeContext } from '../../contexts/ColorModeContext';
import { MOBILE_NAV_HEIGHT } from './shellNav';

const isActive = (item, pathname) => (
  item.matchPrefix ? pathname.startsWith(item.path) : pathname === item.path
);

export function BrandMark({ label }) {
  return (
    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexShrink: 0 }}>
      <Box
        sx={(theme) => ({
          width: 26,
          height: 26,
          borderRadius: '8px',
          display: 'grid',
          placeItems: 'center',
          color: theme.palette.primary.contrastText,
          background: `linear-gradient(135deg, ${theme.palette.primary.main}, ${theme.palette.success.main})`,
        })}
      >
        <ShowChartIcon sx={{ fontSize: 18 }} />
      </Box>
      {label && (
        <Typography component="div" sx={{ fontWeight: 700, fontSize: '15px', letterSpacing: '-0.01em', whiteSpace: 'nowrap' }}>
          {label}
        </Typography>
      )}
    </Box>
  );
}

export function ColorModeButton() {
  const { mode, toggleColorMode } = useContext(ColorModeContext);
  const label = mode === 'dark' ? 'Switch to light mode' : 'Switch to dark mode';
  return (
    <IconButton onClick={toggleColorMode} title={label} aria-label={label} size="small" sx={{ color: 'text.secondary' }}>
      {mode === 'dark' ? <LightModeIcon fontSize="small" /> : <DarkModeIcon fontSize="small" />}
    </IconButton>
  );
}

export function ColorModeListItem({ onDone }) {
  const { mode, toggleColorMode } = useContext(ColorModeContext);
  return (
    <ListItemButton onClick={() => { toggleColorMode(); onDone?.(); }}>
      <ListItemIcon>{mode === 'dark' ? <LightModeIcon /> : <DarkModeIcon />}</ListItemIcon>
      <ListItemText primary={mode === 'dark' ? 'Light mode' : 'Dark mode'} />
    </ListItemButton>
  );
}

export function DesktopNav({ items }) {
  const { pathname } = useLocation();
  return (
    <Box component="nav" aria-label="Main" sx={{ display: 'flex', alignSelf: 'stretch' }}>
      {items.map((item) => {
        const active = isActive(item, pathname);
        return (
          <Button
            key={item.path}
            component={RouterLink}
            to={item.path}
            aria-current={active ? 'page' : undefined}
            sx={{
              color: active ? 'text.primary' : 'text.secondary',
              fontWeight: active ? 600 : 500,
              fontSize: '13px',
              px: 1.5,
              borderRadius: 0,
              borderBottom: '2px solid',
              borderColor: active ? 'primary.main' : 'transparent',
              '&:hover': { color: 'text.primary', backgroundColor: 'action.hover' },
            }}
          >
            {item.label}
          </Button>
        );
      })}
    </Box>
  );
}

// The first four items sit in the bar; the rest, plus `extras(close)`, go in the "More" sheet.
export function MobileBottomNav({ items, extras }) {
  const { pathname } = useLocation();
  const [moreOpen, setMoreOpen] = useState(false);
  const close = () => setMoreOpen(false);
  const primary = items.slice(0, 4);
  const overflow = items.slice(4);
  const activePrimary = primary.find((item) => isActive(item, pathname));
  const value = activePrimary?.path ?? (overflow.some((item) => isActive(item, pathname)) ? 'more' : false);

  return (
    <>
      <Paper
        component="nav"
        aria-label="Main"
        square
        sx={(theme) => ({
          position: 'fixed',
          left: 0,
          right: 0,
          bottom: 0,
          zIndex: theme.zIndex.appBar,
          borderTop: 1,
          borderColor: 'divider',
          pb: 'env(safe-area-inset-bottom)',
          pl: 'env(safe-area-inset-left)',
          pr: 'env(safe-area-inset-right)',
          backgroundColor: alpha(theme.palette.background.paper, 0.92),
          backdropFilter: 'blur(10px)',
        })}
      >
        <BottomNavigation
          showLabels
          value={value}
          sx={{
            height: MOBILE_NAV_HEIGHT,
            backgroundColor: 'transparent',
            '& .MuiBottomNavigationAction-root': { minWidth: 0, color: 'text.secondary' },
            '& .MuiBottomNavigationAction-label': { fontSize: '11px', '&.Mui-selected': { fontSize: '11px', fontWeight: 600 } },
            '& .Mui-selected': { color: 'primary.main' },
          }}
        >
          {primary.map((item) => (
            <BottomNavigationAction
              key={item.path}
              component={RouterLink}
              to={item.path}
              value={item.path}
              label={item.label}
              icon={item.icon}
              aria-current={item === activePrimary ? 'page' : undefined}
            />
          ))}
          <BottomNavigationAction
            value="more"
            label="More"
            icon={<MoreHorizIcon />}
            onClick={() => setMoreOpen(true)}
            aria-haspopup="dialog"
            aria-expanded={moreOpen}
          />
        </BottomNavigation>
      </Paper>

      <Drawer
        anchor="bottom"
        open={moreOpen}
        onClose={close}
        PaperProps={{
          role: 'dialog',
          'aria-label': 'More',
          sx: { borderRadius: '16px 16px 0 0', pb: 'env(safe-area-inset-bottom)' },
        }}
      >
        <Box sx={{ width: 38, height: 4, borderRadius: 2, bgcolor: 'text.disabled', mx: 'auto', mt: 1 }} />
        <List aria-label="More">
          {overflow.map((item) => (
            <ListItemButton
              key={item.path}
              component={RouterLink}
              to={item.path}
              selected={isActive(item, pathname)}
              onClick={close}
            >
              {item.icon && <ListItemIcon>{item.icon}</ListItemIcon>}
              <ListItemText primary={item.label} />
            </ListItemButton>
          ))}
          {overflow.length > 0 && extras && <Divider sx={{ my: 0.5 }} />}
          {extras?.(close)}
        </List>
      </Drawer>
    </>
  );
}
