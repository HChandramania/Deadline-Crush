import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e', fullyParallel: false, workers: 1, timeout: 60000,
  reporter: 'list', use: { baseURL: process.env.PW_LIVE_URL || 'http://127.0.0.1:4173', headless: process.env.PW_INTERACTIVE !== '1', actionTimeout: 30000, trace: 'off', screenshot: 'off', video: 'off' },
  webServer: process.env.PW_LIVE_URL ? undefined : {
    command: 'npm run dev -- --host 127.0.0.1 --port 4173 --strictPort',
    url: 'http://127.0.0.1:4173', reuseExistingServer: false,
    env: { VITE_API_ORIGIN: 'http://127.0.0.1:4173', VITE_NEON_AUTH_URL: 'http://127.0.0.1:4173/auth' },
  },
});
