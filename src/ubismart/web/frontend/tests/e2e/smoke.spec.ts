import { expect, test } from '@playwright/test';

test('壳层加载并展示会话列表', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('header .command-trigger')).toBeVisible();
  await expect(page.locator('.session-item')).toHaveCount(2);
});

test('会话草稿在切换后恢复', async ({ page }) => {
  await page.goto('/');
  const composer = page.locator('.composer textarea');
  await expect(composer).toBeVisible();
  await composer.fill('未发送的草稿');
  await page.waitForTimeout(500);

  // 从“新会话”界面点进已有会话时，草稿跟随到该会话。
  await page.locator('.session-item').filter({ hasText: '未读会话' }).click();
  await expect(composer).toHaveValue('未发送的草稿');

  await page.locator('.session-item').filter({ hasText: '普通会话' }).click();
  await expect(composer).toHaveValue('');

  await page.locator('.session-item').filter({ hasText: '未读会话' }).click();
  await expect(composer).toHaveValue('未发送的草稿');
});

test('语言切换更新壳层文案', async ({ page }) => {
  await page.goto('/');
  await page.locator('header button[aria-label="设置"]').click();
  const settings = page.locator('.settings-modal');
  await expect(settings).toBeVisible();

  const language = settings.locator('select').nth(1);
  await language.selectOption('en-US');
  await expect(page.locator('header button[aria-label="Settings"]')).toBeVisible();
  await expect(settings.locator('#settings-title')).toHaveText('Settings');
});
