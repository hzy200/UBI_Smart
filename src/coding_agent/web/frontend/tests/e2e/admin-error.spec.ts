import { expect, test } from '@playwright/test';

/* 错误路径的 E2E：之前只有 happy path，于是"某个页面接口挂了会怎样"
 * 从来没被浏览器验证过 —— 而那正是错误边界存在的理由。
 *
 * 用 page.route 桩掉接口，比在产物里埋 `?forceError=1` 之类的测试后门好：
 * 后门会跟着进生产。 */

test('管理端某个页面接口失败时，外壳与其它页面仍然可用（不白屏）', async ({
  page,
}) => {
  await page.route(/\/api\/(v\d+\/)?admin\/users/, (route) =>
    route.fulfill({
      status: 500,
      contentType: 'application/json; charset=utf-8',
      body: JSON.stringify({ detail: '控制面数据库不可达' }),
    }),
  );

  await page.goto('/admin');

  // 崩溃/失败被限制在页面里：外壳还在。整棵树被卸载的话这里是空白。
  await expect(page.getByText('企业控制台')).toBeVisible();
  await expect(page.getByRole('button', { name: '用量', exact: true })).toBeVisible();

  // 还能切到别的页 —— 用户不必靠刷新逃生。
  await page.getByRole('button', { name: '用量', exact: true }).click();
  await expect(page.getByText('总 tokens')).toBeVisible();
});

test('管理端接口失败时给出可读提示，而不是静默空白', async ({ page }) => {
  await page.route(/\/api\/(v\d+\/)?admin\/usage/, (route) =>
    route.fulfill({
      status: 500,
      contentType: 'application/json; charset=utf-8',
      body: JSON.stringify({ detail: 'boom' }),
    }),
  );

  await page.goto('/admin');
  await page.getByRole('button', { name: '用量', exact: true }).click();

  // 页面区域内必须有可见内容（错误提示也算），不能是空的。
  await expect(page.locator('.page-wrap')).not.toBeEmpty();
  await expect(page.locator('body')).toContainText(/用量|出错|失败|重试/);
});
