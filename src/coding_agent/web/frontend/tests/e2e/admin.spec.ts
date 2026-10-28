import { expect, test } from '@playwright/test';

test('管理端加载用户列表', async ({ page }) => {
  await page.goto('/admin');
  await expect(page.getByText('企业控制台')).toBeVisible();
  await expect(page.locator('table')).toContainText('alice');
});

test('管理端查看用户额度与 Key', async ({ page }) => {
  await page.goto('/admin');
  await page.getByRole('button', { name: '管理', exact: true }).nth(1).click();
  await expect(page.getByText('每日额度')).toBeVisible();
  await expect(page.getByText('上游模型 Key')).toBeVisible();
  await expect(page.getByText('deepseek', { exact: true })).toBeVisible();
  await expect(page.getByText('登录安全事件')).toBeVisible();
  await expect(page.getByText('登录成功', { exact: true })).toBeVisible();
});

test('管理端切换到用量页', async ({ page }) => {
  await page.goto('/admin');
  await page.getByRole('button', { name: '用量', exact: true }).click();
  await expect(page.getByText('总 tokens')).toBeVisible();
  await expect(page.locator('table')).toContainText('alice');
  await expect(page.locator('.stat-grid')).toContainText('¥');
});
