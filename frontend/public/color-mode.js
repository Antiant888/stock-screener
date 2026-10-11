// Runs before the app bundle so the first paint uses the right background.
// Mirrors readColorMode() in src/theme.js; a file rather than an inline script
// because the nginx CSP only allows script-src 'self'.
(function () {
  var mode = null;
  try {
    mode = localStorage.getItem('stockscanner-color-mode');
  } catch {
    // Storage blocked: fall through to the OS preference, as readColorMode does.
  }
  if (mode !== 'light' && mode !== 'dark') {
    mode = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  }
  document.documentElement.dataset.colorMode = mode;
  var meta = document.querySelector('meta[name="theme-color"]');
  // background.paper per mode in src/theme.js.
  if (meta) meta.setAttribute('content', mode === 'light' ? '#ffffff' : '#12151c');
})();
