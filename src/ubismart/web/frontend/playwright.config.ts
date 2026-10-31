import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests/e2e',
  timeout: 30_000,
  expect: { timeout: 8_000 },
  fullyParallel: false,
  reporter: 'list',
  use: {
    baseURL: 'http://127.0.0.1:4173',
    trace: 'retain-on-failure',
    // 界面默认跟随浏览器语言；CI runner 是 en-US，会让中文断言失败。
    // 固定成中文，保证测试与产品默认文案一致。
    locale: 'zh-CN',
  },
  projects: [
    {
      name: 'chrome',
      use: {
        ...devices['Desktop Chrome'],
        // 使用系统 Chrome，避免 CI/本地额外下载 Playwright 浏览器。
        channel: 'chrome',
      },
    },
  ],
  webServer: {
    command: 'node tests/e2e/server.mjs',
    url: 'http://127.0.0.1:4173/healthz',
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
});
