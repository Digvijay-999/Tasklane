/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react';
import { defineConfig, type Plugin } from 'vite';

// The browser only ever talks to this dev/preview server; it forwards /v1 and /health to the API
// (same origin for the page, fetch and EventSource, so the API needs no CORS headers).
// Inside Docker the API is http://api:8000; on Windows it is http://127.0.0.1:8000.
const apiTarget = process.env.TASKLANE_API_URL ?? 'http://127.0.0.1:8000';
const proxy = {
  '/v1': { target: apiTarget },
  '/health': { target: apiTarget },
};

// Production Content-Security-Policy: only this origin, no inline scripts or styles, no
// plugins, no framing. The built index.html carries it as a <meta> tag (travels with the static
// files); `vite preview` also sends it as a header, where frame-ancestors is allowed.
const CSP = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self'",
  "img-src 'self'",
  "font-src 'self'",
  "connect-src 'self'",
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'self'",
].join('; ');

function cspMeta(): Plugin {
  return {
    name: 'tasklane-csp-meta',
    apply: 'build', // dev mode needs inline scripts for hot reload, so only the build gets it
    transformIndexHtml: () => [
      { tag: 'meta', attrs: { 'http-equiv': 'Content-Security-Policy', content: CSP }, injectTo: 'head-prepend' },
    ],
  };
}

export default defineConfig({
  plugins: [react(), cspMeta()],
  build: {
    assetsInlineLimit: 0, // never inline assets as data: URIs (the CSP allows only 'self')
  },
  server: {
    // 127.0.0.1 on Windows. In the container it must listen on 0.0.0.0 to be reachable through
    // Docker's port mapping, which compose.yaml publishes on 127.0.0.1 only.
    host: process.env.TASKLANE_DEV_HOST ?? '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy,
    // File change events don't cross from Windows into Linux containers: poll instead.
    watch: process.env.TASKLANE_DEV_POLLING === 'true' ? { usePolling: true, interval: 300 } : undefined,
  },
  preview: {
    host: '127.0.0.1',
    port: 4173,
    strictPort: true,
    proxy,
    headers: { 'Content-Security-Policy': `${CSP}; frame-ancestors 'none'` },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    restoreMocks: true,
  },
});
