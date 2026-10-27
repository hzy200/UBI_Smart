/* jsdom 交互测试：真实加载 static 构建产物、Alpine CSP 与桩 API。 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { startApp, waitFor } from './harness';

function keydown(window: any, init: any) {
  const event = new window.KeyboardEvent('keydown', {
    bubbles: true,
    cancelable: true,
    ...init,
  });
  window.dispatchEvent(event);
  return event;
}

test('jsdom 交互：会话、文件树、Problems、流式渲染与焦点陷阱', async (t) => {
  const app = await startApp();
  const { window, document, data } = app;
  try {
    await t.test('会话列表显示未读/摘要并支持批量选择', async () => {
      await waitFor(
        () => document.querySelectorAll('.session-item').length === 2,
        '会话列表',
      );
      const first = document.querySelector('.session-item') as HTMLElement;
      assert.ok(first.classList.contains('unread'));
      assert.equal(
        first.querySelector('.session-preview')?.textContent?.trim(),
        '上一次运行的最后一条回答',
      );
      assert.match(
        first.querySelector('.m')?.textContent || '',
        /2 轮 · 3 次工具/,
      );

      const check = first.querySelector('.session-check') as HTMLInputElement;
      check.click();
      assert.equal(data.sessionSelected['thread-dom-test'], true);
      assert.equal(data.hasSessionSelection, true);
      await waitFor(
        () => document.querySelector('.session-batch')?.style.display !== 'none',
        '批量操作栏',
      );

      const clear = document.querySelector(
        '.session-batch button[data-op="clear"]',
      ) as HTMLElement;
      clear.click();
      assert.equal(data.hasSessionSelection, false);

      data.markSessionRead('thread-dom-test');
      await waitFor(
        () => !first.classList.contains('unread'),
        '标记已读',
      );
    });

    await t.test('文件树显示 Git M/A/D/U 标记与目录聚合', async () => {
      data.setView('tree');
      await waitFor(
        () => document.querySelectorAll('.tree-row').length >= 3,
        '文件树行',
      );
      const rowFor = (name: string) => {
        const rows = Array.from(document.querySelectorAll('.tree-row'));
        return rows.find((row) => (
          row.querySelector('.tree-name')?.textContent?.trim() === name
        ));
      };
      assert.equal(rowFor('a.py')?.querySelector('.tree-git')?.textContent?.trim(), 'M');
      assert.equal(rowFor('new.txt')?.querySelector('.tree-git')?.textContent?.trim(), 'U');
      assert.equal(rowFor('src')?.querySelector('.tree-git')?.textContent?.trim(), '●');
      assert.match(
        rowFor('src')?.querySelector('.tree-git')?.getAttribute('title') || '',
        /未提交改动/,
      );
    });

    await t.test('Problems 按文件与来源聚合并可折叠', async () => {
      data.problems = [];
      data.problemExpanded = {};
      data.addProblem('review', 'error', '导入错误', 'src/a.py:12');
      data.addProblem('review', 'warning', '慢循环', 'src/a.py:20');
      data.addProblem('verification', 'error', '测试失败', '');

      await waitFor(
        () => document.querySelectorAll('.problem-group').length === 2,
        '问题分组',
      );
      const groups = data.problemGroups;
      assert.equal(groups[0]._label, 'src/a.py');
      assert.equal(groups[0]._countText, '1 错误 / 1 警告');
      assert.equal(groups[1]._label, '验证');

      const head = document.querySelector(
        '.problem-group-head[data-group="file:src/a.py"]',
      ) as HTMLElement;
      head.click();
      await waitFor(() => data.problemGroups[0]._open === false, '分组折叠');
      assert.equal(document.querySelectorAll('.problem-group-items').length, 1);

      head.click();
      await waitFor(() => data.problemGroups[0]._open === true, '分组展开');
      assert.equal(document.querySelectorAll('.problem-group-items').length, 2);
    });

    await t.test('流式 token 增量渲染 Markdown', async () => {
      const before = data.messages.length;
      data.appendAssistantToken('dom-node-1', 'hello');
      await waitFor(() => {
        const message = data.messages[data.messages.length - 1];
        return message && message.text === 'hello' && message.html.includes('hello');
      }, '首个 token');

      data.appendAssistantToken(
        'dom-node-1',
        ' world\n\n```js\nconst a = 1;\n',
      );
      await waitFor(() => {
        const message = data.messages[data.messages.length - 1];
        return message && message.text.includes('const a = 1');
      }, '后续 token');

      const message = data.messages[data.messages.length - 1];
      assert.equal(data.messages.length, before + 1);
      assert.match(message.html, /hello/);
      assert.match(message.html, /const a = 1/);
      data.finishAssistant();
    });

    await t.test('设置模态焦点进入、Tab 循环、Esc 关闭并恢复', async () => {
      const opener = document.querySelector(
        'header .icon-btn[aria-label="打开设置"]',
      ) as HTMLElement;
      assert.ok(opener);
      opener.focus();
      opener.click();
      const modal = document.querySelector('.settings-modal') as HTMLElement;
      await waitFor(() => modal.style.display !== 'none', '设置打开');
      await waitFor(() => modal.contains(document.activeElement), '焦点进入设置', 2000);

      const items = data.modalFocusables(modal);
      assert.ok(items.length >= 2);

      items[items.length - 1].focus();
      keydown(window, { key: 'Tab' });
      assert.equal(document.activeElement, items[0]);

      items[0].focus();
      keydown(window, { key: 'Tab', shiftKey: true });
      assert.equal(document.activeElement, items[items.length - 1]);

      keydown(window, { key: 'Escape' });
      await waitFor(() => modal.style.display === 'none', '设置关闭');
      await waitFor(() => document.activeElement === opener, '焦点恢复');
    });

    await t.test('命令面板焦点陷阱与 Esc 恢复触发元素', async () => {
      const trigger = document.querySelector('.command-trigger') as HTMLElement;
      assert.ok(trigger);
      trigger.focus();
      trigger.click();
      await waitFor(() => data.commandOpen === true, '命令面板打开');
      const input = document.querySelector(
        '.command-input-row input',
      ) as HTMLElement;
      await waitFor(() => document.activeElement === input, '命令输入聚焦');

      const modal = document.querySelector('.command-modal') as HTMLElement;
      const items = data.modalFocusables(modal);
      items[items.length - 1].focus();
      keydown(window, { key: 'Tab' });
      assert.equal(document.activeElement, items[0]);

      keydown(window, { key: 'Escape' });
      await waitFor(() => data.commandOpen === false, '命令面板关闭');
      await waitFor(() => document.activeElement === trigger, '焦点回到触发元素');
    });

    await t.test('没有 Alpine CSP 表达式错误', () => {
      const errors = app.warnings.filter(
        (line) => line.includes('unable to interpret'),
      );
      assert.deepEqual(errors, []);
    });
  } finally {
    await app.close();
  }
});
