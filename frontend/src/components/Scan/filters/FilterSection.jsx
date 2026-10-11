import { useState } from 'react';
import {
  Box,
  Typography,
  Collapse,
  IconButton,
  Chip,
} from '@mui/material';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import ExpandLessIcon from '@mui/icons-material/ExpandLess';
import AccountBalanceIcon from '@mui/icons-material/AccountBalance';
import ShowChartIcon from '@mui/icons-material/ShowChart';
import StarIcon from '@mui/icons-material/Star';
import { onPalette } from '../../../utils/colorUtils';

// Neutral section chrome; the theme accent marks only the icon and the
// active-filter count, so the sections don't compete with the results.
const CATEGORY_ICONS = {
  fundamental: AccountBalanceIcon,
  technical: ShowChartIcon,
  rating: StarIcon,
};

/**
 * Collapsible filter section with category-specific styling
 */
function FilterSection({
  title,
  category = 'fundamental',
  activeCount = 0,
  defaultExpanded = true,
  children
}) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const IconComponent = CATEGORY_ICONS[category] || CATEGORY_ICONS.fundamental;

  return (
    <Box
      sx={{
        mb: 1.5,
        borderRadius: 1,
        border: '1px solid',
        borderColor: 'divider',
        backgroundColor: 'background.subtle',
        overflow: 'hidden',
      }}
    >
      {/* Section Header */}
      <Box
        onClick={() => setExpanded(!expanded)}
        sx={{
          display: 'flex',
          alignItems: 'center',
          px: 1.5,
          py: 0.75,
          cursor: 'pointer',
          userSelect: 'none',
          '&:hover': {
            backgroundColor: 'action.hover',
          },
        }}
      >
        <IconComponent
          sx={{
            fontSize: 16,
            mr: 1,
            color: 'primary.main',
          }}
        />
        <Typography
          variant="subtitle2"
          sx={{
            fontSize: '0.75rem',
            fontWeight: 600,
            color: 'text.primary',
            textTransform: 'uppercase',
            letterSpacing: '0.5px',
            flexGrow: 1,
          }}
        >
          {title}
        </Typography>

        {activeCount > 0 && (
          <Chip
            label={`${activeCount} active`}
            size="small"
            sx={{
              height: 18,
              fontSize: '0.65rem',
              fontWeight: 500,
              ...onPalette('primary.main'),
              mr: 1,
              '& .MuiChip-label': { px: 1 },
            }}
          />
        )}

        <IconButton
          size="small"
          sx={{
            p: 0.25,
            color: 'text.secondary',
          }}
          onClick={(e) => {
            e.stopPropagation();
            setExpanded(!expanded);
          }}
        >
          {expanded ? (
            <ExpandLessIcon sx={{ fontSize: 18 }} />
          ) : (
            <ExpandMoreIcon sx={{ fontSize: 18 }} />
          )}
        </IconButton>
      </Box>

      {/* Section Content */}
      <Collapse in={expanded}>
        <Box sx={{ px: 1.5, pb: 1.5, pt: 0.5 }}>
          {children}
        </Box>
      </Collapse>
    </Box>
  );
}

export default FilterSection;
