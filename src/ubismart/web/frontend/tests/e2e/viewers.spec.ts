import { expect, test } from '@playwright/test';
import { readFileSync } from 'node:fs';

const FIXTURES = new URL('./fixtures/', import.meta.url);

const FILES = [
  { name: 'a.py', path: 'a.py', is_dir: false, size: 9 },
  { name: 'note.md', path: 'note.md', is_dir: false, size: 20 },
  { name: 'doc.docx', path: 'doc.docx', is_dir: false, size: 10 },
  { name: 'sheet.xlsx', path: 'sheet.xlsx', is_dir: false, size: 10 },
  { name: 'large.xlsx', path: 'large.xlsx', is_dir: false, size: 10 },
  { name: 'slides.pptx', path: 'slides.pptx', is_dir: false, size: 10 },
  { name: 'old.doc', path: 'old.doc', is_dir: false, size: 10 },
  { name: 'icon.svg', path: 'icon.svg', is_dir: false, size: 10 },
  { name: 'a.pdf', path: 'a.pdf', is_dir: false, size: 10 },
];

const DOCUMENTS: Record<string, unknown> = {
  'doc.docx': {
    path: 'doc.docx',
    format: 'docx',
    blocks: [
      { kind: 'text', anchor: 'paragraph=1', text: 'Word 正文' },
      { kind: 'table', anchor: 'table=1', text: '表格', rows: [['A', 'B']] },
      { kind: 'header', anchor: 'header=1', text: '页眉内容' },
      { kind: 'note', anchor: 'comment=1', text: '[审阅人] 批注内容' },
    ],
    warnings: [],
  },
  'sheet.xlsx': {
    path: 'sheet.xlsx',
    format: 'xlsx',
    blocks: [
      {
        kind: 'sheet',
        anchor: 'sheet=数据!A1:B2',
        text: '工作表 数据',
        rows: [['名称', '数量'], ['苹果', '3']],
      },
    ],
    warnings: [],
  },
  'slides.pptx': {
    path: 'slides.pptx',
    format: 'pptx',
    blocks: [{ kind: 'slide', anchor: 'slide=1', text: '第一页' }],
    slide_layouts: [{
      page: 1,
      width: 12192000,
      height: 6858000,
      svg:
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12192000 6858000">'
        + '<rect width="12192000" height="6858000" fill="#ffffff"/>'
        + '<text x="500000" y="800000" font-size="400000" fill="#111111">第一页</text>'
        + '</svg>',
    }],
    warnings: [],
  },
};

async function prepare(page: import('@playwright/test').Page) {
  await page.route(/\/api\/(?:v\d+\/)?files(?:[?/]|$)/, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ path: '', entries: FILES, truncated: false }),
    }),
  );
  await page.route(/\/api\/(?:v\d+\/)?file(?:[?/]|$)/, (route) => {
    const url = new URL(route.request().url());
    const pathname = url.pathname;
    const path = url.searchParams.get('path') || '';
    if (pathname.endsWith('/file/document')) {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(DOCUMENTS[path] || DOCUMENTS['doc.docx']),
      });
    }
    if (pathname.endsWith('/file/raw')) {
      const fixture = path === 'large.xlsx'
        ? 'large.xlsx'
        : path.endsWith('.docx')
          ? 'sample.docx'
          : path.endsWith('.xlsx') || path.endsWith('.xls')
            ? 'sample.xlsx'
            : 'sample.pdf';
      return route.fulfill({
        status: 200,
        contentType: fixture.endsWith('.pdf')
          ? 'application/pdf'
          : 'application/octet-stream',
        body: readFileSync(new URL(fixture, FIXTURES)),
      });
    }
    const body = path.endsWith('.md')
      ? { path, content: '# 标题\n\n正文', size: 20, binary: false, truncated: false, image: false, mime: '', data_url: '', image_too_large: false }
      : path.endsWith('.svg')
        ? { path, content: '<svg xmlns="http://www.w3.org/2000/svg"/>', size: 40, binary: false, truncated: false, image: true, mime: 'image/svg+xml', data_url: 'data:image/svg+xml;base64,PHN2Zy8+', image_too_large: false }
        : { path, content: 'print(1)\n', size: 9, binary: false, truncated: false, image: false, mime: '', data_url: '', image_too_large: false };
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(body),
    });
  });

  await page.goto('/');
  await page.locator('.session-item').first().click();
  await page.getByRole('button', { name: '文件树' }).click();
  await expect(page.locator('.tree-row')).toHaveCount(FILES.length);
}

async function openFile(page: import('@playwright/test').Page, path: string) {
  await page.locator(`.tree-row[data-path="${path}"]`).click();
}

test('代码与 Markdown 阅读器', async ({ page }) => {
  await prepare(page);
  await openFile(page, 'a.py');
  await expect(page.locator('.editor-code')).toContainText('print(1)');

  await openFile(page, 'note.md');
  await expect(page.locator('.markdown-body h1:visible')).toHaveText('标题');
});

test('Office 结构化阅读器与旧格式提示', async ({ page }) => {
  await prepare(page);
  await openFile(page, 'doc.docx');
  await expect(page.locator('.markdown-body:visible')).toContainText('Word 正文');
  await expect(page.locator('.docx-extras')).toContainText('页眉内容');
  await expect(page.locator('.docx-extras')).toContainText('批注内容');

  await openFile(page, 'sheet.xlsx');
  await expect(page.locator('.document-preview')).toContainText('苹果');
  await page.locator('td[data-row="1"][data-col="2"]').click();
  await expect(page.locator('.xlsx-formula')).toContainText('=B2*2');
  await page.getByRole('button', { name: '汇总' }).click();
  await expect(page.locator('.document-preview')).toContainText('总计');

  await openFile(page, 'slides.pptx');
  await expect(page.locator('.slide-svg')).toContainText('第一页');

  await openFile(page, 'old.doc');
  await expect(page.locator('.unsupported-preview')).toContainText(
    '请先另存为',
  );
});

test('XLSX 大表按可视窗口渲染并可读取底部单元格', async ({ page }) => {
  await prepare(page);
  await openFile(page, 'large.xlsx');

  const rows = page.locator('.document-table tbody tr');
  await expect(rows.first()).toBeVisible();
  expect(await rows.count()).toBeLessThan(500);

  const wrap = page.locator('.document-table-wrap').first();
  await wrap.evaluate((el) => {
    el.scrollTop = el.scrollHeight;
    el.dispatchEvent(new Event('scroll', { bubbles: true }));
  });
  await expect(page.locator('td[data-row="499"]').first()).toBeVisible();
  expect(await rows.count()).toBeLessThan(500);

  await page.locator('td[data-row="499"][data-col="1"]').click();
  await expect(page.locator('.xlsx-formula')).toContainText('B500');
  await expect(page.locator('.xlsx-formula')).toContainText('499');
});

test('PDF 使用独立 viewer，SVG 只按源码展示', async ({ page }) => {
  await prepare(page);
  await openFile(page, 'a.pdf');
  await expect(page.locator('.pdf-canvas')).toBeVisible();
  await expect(page.locator('.pdf-text-layer')).toContainText('PDF page');
  await expect(page.locator('.pdf-text-layer')).toContainText('Rotated text');
  await expect(page.locator('.pdf-meta')).toContainText('PDF 样本');
  await expect(
    page.getByRole('button', { name: /First page/ }),
  ).toBeVisible();
  await page.getByRole('button', { name: '放大' }).click();
  await expect(page.locator('.pdf-canvas')).toBeVisible();

  await page.locator('input[placeholder="搜索"]').fill('second');
  await page.getByRole('button', { name: '搜索', exact: true }).click();
  await expect(page.locator('.pdf-search-label')).toContainText('1/1');
  await expect(page.locator('.pdf-hit').first()).toBeVisible();

  await page.getByRole('button', { name: '连续' }).click();
  await expect(page.locator('.pdf-continuous .pdf-page')).toHaveCount(5);
  await page.getByRole('button', { name: /Last page/ }).click();
  await expect(page.locator('.pdf-continuous .pdf-page')).toHaveCount(25);
  await expect(
    page.getByRole('button', { name: '单页' }),
  ).toBeVisible();

  await openFile(page, 'icon.svg');
  await expect(page.locator('.editor-code')).toContainText('<svg');
  await expect(page.locator('.image-preview')).toBeHidden();
});
