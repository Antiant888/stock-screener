import { existsSync, readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');

describe('the Inter font', () => {
  const css = read('./index.css');
  const faces = css.match(/@font-face\s*{[^}]*}/g) ?? [];

  it('is not fetched from a third-party stylesheet', () => {
    // A CSS @import blocks first paint until another origin answers.
    expect(css).not.toMatch(/@import\s+url\(\s*['"]?https?:/);
  });

  it('comes from bundled files and never swaps in late', () => {
    expect(faces.length).toBeGreaterThan(0);
    for (const face of faces) {
      expect(face).toMatch(/font-family:\s*'Inter'/);
      expect(face).toMatch(/font-display:\s*optional/);
      const src = face.match(/url\(\s*['"]?([^'")]+)['"]?\s*\)/)[1];
      expect(src).not.toMatch(/^https?:/);
      expect(existsSync(new URL(src, import.meta.url))).toBe(true);
    }
  });

  it('is preloaded from the page so it is ready by first paint', () => {
    const preload = read('../index.html').match(/<link[^>]*inter-latin\.woff2[^>]*>/)?.[0] ?? '';
    expect(preload).toMatch(/rel="preload"/);
    expect(preload).toMatch(/as="font"/);
    expect(preload).toMatch(/type="font\/woff2"/);
    // Fonts are always fetched in CORS mode; without this the preload is wasted.
    expect(preload).toMatch(/crossorigin/);
  });

  it('is cached as immutable like every other hashed asset', () => {
    // The font lands in /assets/ with a content hash; without ^~ nginx picks
    // the later woff2 regex block (30 days, no immutable, no nosniff).
    expect(read('../nginx.conf')).toMatch(/location \^~ \/assets\/ \{[^}]*immutable/);
  });

  it('needs no Google origins in the content security policy', () => {
    const policy = read('../nginx.conf').match(/Content-Security-Policy "([^"]+)"/)[1];
    expect(policy).not.toMatch(/fonts\.googleapis\.com|fonts\.gstatic\.com/);
  });
});
