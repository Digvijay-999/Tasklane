import { defineConfig, devices } from '@playwright/test';

// End-to-end tests against the REAL stack (docker compose: API, dispatcher, worker, Postgres,
// Redis) through the production build served by `vite preview` (so the CSP is enforced too).
// The stack must run with the demo settings; see WALKTHROUGH_PHASE9.md, "Run the tests".
export default defineConfig({
  testDir: 'e2e',
  globalSetup: './e2e/global-setup.ts',
  fullyParallel: false,
  workers: 1, // one worker container processes jobs; parallel tests would only queue behind it
  timeout: 180_000,
  expect: { timeout: 15_000 },
  reporter: [['list']],
  use: {
    baseURL: process.env.TASKLANE_UI_URL ?? 'http://127.0.0.1:4173',
    trace: 'retain-on-failure',
    ...devices['Desktop Chrome'],
    viewport: { width: 1360, height: 900 },
  },
  projects: [
    { name: 'e2e', testIgnore: /screenshots\.spec\.ts/ },
    { name: 'screenshots', testMatch: /screenshots\.spec\.ts/ },
  ],
  webServer: {
    command: 'npm run build && npx vite preview',
    url: 'http://127.0.0.1:4173',
    reuseExistingServer: false, // always test a fresh build of the current source
    timeout: 120_000,
  },
});
