const { defineConfig } = require('@playwright/test');
module.exports = defineConfig({
  testDir: './tests/browser',
  timeout: 20000,
  workers: 1,
  use: { baseURL: 'http://127.0.0.1:18042', headless: true },
  webServer: {
    command: 'python3 tests/browser/fixture_server.py',
    url: 'http://127.0.0.1:18042/health',
    reuseExistingServer: false,
    timeout: 15000,
  },
});
