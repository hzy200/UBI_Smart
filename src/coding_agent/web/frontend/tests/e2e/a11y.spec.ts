import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';

/* 无障碍自动化：用 axe-core 扫用户端与管理端。
 *
 * 颜色对比度在本地主题/系统字体下噪声较大，先禁用；其余 WCAG A/AA
 * 结构性问题（名称、标签、landmark、重复 id 等）保持零容忍。 */

async function scan(page: import('@playwright/test').Page) {
  const results = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa'])
    .disableRules(['color-contrast'])
    .analyze();
  return results.violations.map((item) => ({
    id: item.id,
    impact: item.impact,
    nodes: item.nodes.map((node) => node.target.join(' ')),
  }));
}

test('用户端主页没有结构性无障碍问题', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('body')).toBeVisible();
  expect(await scan(page)).toEqual([]);
});

test('管理端登录/外壳没有结构性无障碍问题', async ({ page }) => {
  await page.route(/\/api\/(v\d+\/)?me$/, (route) =>
    route.fulfill({
      status: 401,
      contentType: 'application/json',
      body: JSON.stringify({ detail: '未登录' }),
    }),
  );
  await page.goto('/admin');
  await expect(page.locator('body')).toBeVisible();
  expect(await scan(page)).toEqual([]);
});
