import { describe, expect, it } from 'vitest';

import { renderWithProviders } from '../../test/renderWithProviders';
import PriceSparkline from './PriceSparkline';
import RSSparkline from './RSSparkline';

const numbers = (d) => d.match(/-?\d+(\.\d+)?/g).map(Number);

describe('PriceSparkline', () => {
  it('draws a 1.5px line that stays 1.5px however wide the chart is stretched', () => {
    const { container } = renderWithProviders(<PriceSparkline data={[10, 9, 11]} trend={1} height={28} />);
    const line = container.querySelector('path[fill="none"]');

    expect(line.getAttribute('stroke-width')).toBe('1.5');
    expect(line.getAttribute('vector-effect')).toBe('non-scaling-stroke');
    // 2px margins: the extremes sit at y=2 (max) and y=26 (min) within 28.
    const ys = numbers(line.getAttribute('d')).filter((_, i) => i % 2 === 1);
    expect(Math.min(...ys)).toBeCloseTo(3.091, 3);
    expect(Math.max(...ys)).toBeCloseTo(24.909, 3);
  });

  it('fills down to zero when the padded range dips below it, as recharts did', () => {
    // [0.2, 10, 20]: padding 0.99 gives the domain [-0.79, 20.99], so the
    // fill stops at 0 rather than the padded floor (y=26).
    const { container } = renderWithProviders(<PriceSparkline data={[0.2, 10, 20]} trend={1} height={28} />);
    const area = container.querySelector('path:not([fill="none"])');
    const zeroY = 26 - (0.79 / 21.78) * 24;

    expect(area.getAttribute('d')).toMatch(/L1000,([\d.]+)L0,([\d.]+)Z$/);
    const [, closeY] = area.getAttribute('d').match(/L0,([\d.]+)Z$/);
    expect(Number(closeY)).toBeCloseTo(zeroY, 2);
  });

  it('marks a single point with a centred dot, as recharts did', () => {
    const { container } = renderWithProviders(<PriceSparkline data={[42]} trend={0} height={28} />);
    const dot = container.querySelector('circle');

    expect(dot.getAttribute('cx')).toBe('50%');
    expect(dot.getAttribute('cy')).toBe('14');
    expect(dot.getAttribute('r')).toBe('3');
    expect(dot.getAttribute('stroke-width')).toBe('1.5');
    expect(dot.getAttribute('fill-opacity')).toBe('0.25');
    expect(container.querySelectorAll('path')).toHaveLength(0);
  });
});

describe('RSSparkline', () => {
  it('draws one rounded bar per value, the highest and lowest at full opacity', () => {
    const { container } = renderWithProviders(<RSSparkline data={[10, 9, 11]} trend={1} width={60} height={20} />);
    const bars = [...container.querySelectorAll('svg path')];

    expect(bars).toHaveLength(3);
    expect(bars.map((bar) => bar.getAttribute('fill-opacity'))).toEqual(['0.55', '1', '1']);
    expect(numbers(bars[0].getAttribute('d')).slice(0, 2)).toEqual([8.5, 11]);
  });
});
