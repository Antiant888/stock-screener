/**
 * RRG filter controls: a quadrant multi-select + a current-rank range slider +
 * a name multi-select. Presentational only — state lives in useRRGFilters.
 * Shared by the live and static Group Rankings pages via RRGChart.
 */
import {
  Autocomplete,
  TextField,
  Slider,
  Box,
  Typography,
  ToggleButton,
  ToggleButtonGroup,
} from '@mui/material';
import { QUADRANTS, QUADRANT_TONES } from './rrgColors';

export default function RRGFilters({
  scopeLabel,
  names,
  selected,
  onSelected,
  quadrants,
  onQuadrants,
  maxRank,
  rankValue,
  onRankChange,
}) {
  return (
    <>
      {/* Quadrant multi-select — empty selection means "all shown". Each button
          is tinted with its quadrant color when active. */}
      <ToggleButtonGroup
        size="small"
        value={quadrants}
        onChange={(_e, v) => onQuadrants(v)}
        aria-label="Filter by quadrant"
      >
        {QUADRANTS.map((q) => (
          <ToggleButton
            key={q}
            value={q}
            sx={{
              textTransform: 'none',
              '&.Mui-selected': {
                color: `${QUADRANT_TONES[q]}.main`,
                borderColor: `${QUADRANT_TONES[q]}.main`,
                fontWeight: 700,
              },
            }}
          >
            {q}
          </ToggleButton>
        ))}
      </ToggleButtonGroup>
      {maxRank > 1 && (
        <Box sx={{ width: 200 }}>
          <Typography variant="caption" color="text.secondary">
            Rank {rankValue[0]}–{rankValue[1]}
          </Typography>
          <Slider
            size="small"
            min={1}
            max={maxRank}
            value={rankValue}
            onChange={(_e, v) => onRankChange(v)}
            valueLabelDisplay="auto"
            disableSwap
            sx={{ mt: -0.5 }}
          />
        </Box>
      )}
      <Autocomplete
        multiple
        size="small"
        options={names}
        value={selected}
        onChange={(_e, v) => onSelected(v)}
        limitTags={3}
        disableCloseOnSelect
        renderInput={(params) => (
          <TextField
            {...params}
            label={`Filter ${scopeLabel.toLowerCase()}`}
            placeholder={selected.length ? '' : 'All shown'}
          />
        )}
        sx={{ width: 360, maxWidth: '100%' }}
      />
    </>
  );
}
