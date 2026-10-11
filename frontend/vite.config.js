import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';

import { optionsValidatorsPlugin } from './build/optionsValidatorsPlugin.js';

const STATIC_TITLE = 'Stock Scanner Daily: free daily stock scans, breadth and group rankings';
const STATIC_DESCRIPTION = 'Free, open-source daily stock scans for 12 markets: Minervini, CANSLIM and VCP screens, '
  + 'market breadth, IBD-style group rankings and relative rotation. Updated after each market close.';

// Search and link-preview tags for the public static demo only; the self-hosted
// app keeps its plain index.html. og:image must be absolute, so it needs VITE_SITE_URL.
const staticSiteMetaPlugin = (siteUrl) => ({
  name: 'static-site-meta',
  transformIndexHtml(html) {
    const meta = [
      ['name', 'description', STATIC_DESCRIPTION],
      ['property', 'og:type', 'website'],
      ['property', 'og:site_name', 'Stock Scanner Daily'],
      ['property', 'og:title', STATIC_TITLE],
      ['property', 'og:description', STATIC_DESCRIPTION],
      ['name', 'twitter:card', 'summary_large_image'],
      ...(siteUrl ? [
        ['property', 'og:url', siteUrl],
        ['property', 'og:image', `${siteUrl}og-image.png`],
        ['property', 'og:image:width', '1200'],
        ['property', 'og:image:height', '630'],
      ] : []),
    ];
    return {
      html: html.replace(/<title>.*<\/title>/, `<title>${STATIC_TITLE}</title>`),
      tags: meta.map(([key, value, content]) => ({
        tag: 'meta', attrs: { [key]: value, content }, injectTo: 'head',
      })),
    };
  },
});

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, fileURLToPath(new URL('.', import.meta.url)), '');
  const configuredBase = env.VITE_BASE_PATH || '/';
  const base = configuredBase.endsWith('/') ? configuredBase : `${configuredBase}/`;
  const siteUrl = env.VITE_SITE_URL && (env.VITE_SITE_URL.endsWith('/') ? env.VITE_SITE_URL : `${env.VITE_SITE_URL}/`);

  return {
    base,
    plugins: [
      react(),
      optionsValidatorsPlugin(),
      ...(env.VITE_STATIC_SITE === 'true' ? [staticSiteMetaPlugin(siteUrl)] : []),
    ],
    test: {
      environment: 'jsdom',
      globals: true,
      setupFiles: './src/test/setup.js',
      css: false,
      testTimeout: 20000,
      hookTimeout: 20000,
      include: ['src/**/*.test.{js,jsx}'],
    },
    server: {
      port: 5173,
      open: true,
      proxy: {
        '/api': {
          target: 'http://127.0.0.1:8000',
          changeOrigin: true,
        },
      },
    },
    build: {
      // Manual chunks for better caching and smaller initial bundle
      rollupOptions: {
        output: {
          manualChunks: {
            // Core React ecosystem
            // Query's React adapters share React's initialization boundary.
            // Splitting these creates a circular vendor chunk in production.
            'react-vendor': [
              'react', 'react-dom', 'react-router-dom',
              '@tanstack/react-query', '@tanstack/react-virtual',
              '@tanstack/react-query-persist-client', '@tanstack/query-sync-storage-persister',
            ],
            // MUI components (large library)
            'mui-vendor': ['@mui/material', '@mui/icons-material'],
            // Data visualization: recharts powers the always-visible
            // sparklines; lightweight-charts only loads with the (lazy)
            // candlestick chart modals.
            'charts-vendor': ['recharts'],
            'price-charts-vendor': ['lightweight-charts'],
          },
        },
      },
      // Increase chunk size warning limit
      chunkSizeWarningLimit: 1000,
      // Enable source maps for production debugging
      sourcemap: false,
    },
    // Optimize dependencies
    optimizeDeps: {
      include: [
        'react',
        'react-dom',
        'react-router-dom',
        '@mui/material',
        '@tanstack/react-query',
        '@tanstack/react-virtual',
        'recharts',
      ],
    },
  };
});
