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

test('jsdom 交互：会话、文件树、Problems、流式、审批与布局', async (t) => {
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

    await t.test('文件树与主区标签支持键盘导航', async () => {
      const rows = () => Array.from(
        document.querySelectorAll('.tree-row'),
      ) as HTMLElement[];
      const first = rows()[0];
      first.focus();
      assert.equal(document.activeElement, first);
      first.dispatchEvent(new window.KeyboardEvent('keydown', {
        key: 'ArrowDown',
        bubbles: true,
        cancelable: true,
      }));
      await waitFor(
        () => document.activeElement === rows()[1],
        '树焦点下移',
      );

      const srcRow = rows().find(
        (row) => row.getAttribute('data-path') === 'src',
      ) as HTMLElement;
      srcRow.focus();
      srcRow.dispatchEvent(new window.KeyboardEvent('keydown', {
        key: 'ArrowRight',
        bubbles: true,
        cancelable: true,
      }));
      await waitFor(() => rows().length === 4, '展开目录');
      assert.equal(
        rows()[0].querySelector('.tree-icon use')?.getAttribute('href'),
        '#i-file-code',
      );
      const child = rows().find(
        (row) => row.getAttribute('data-path') === 'src/b.ts',
      ) as HTMLElement;
      assert.equal(child.querySelectorAll('.tree-guide').length, 1);
      srcRow.dispatchEvent(new window.KeyboardEvent('keydown', {
        key: 'ArrowLeft',
        bubbles: true,
        cancelable: true,
      }));
      await waitFor(() => rows().length === 3, '折叠目录');

      await data.openFile('a.py');
      await waitFor(
        () => data.mainTab === 'file' && data.editorTabs.length === 1,
        '打开文件标签',
      );
      const chatTab = document.querySelector('.main-tab') as HTMLElement;
      chatTab.focus();
      chatTab.dispatchEvent(new window.KeyboardEvent('keydown', {
        key: 'ArrowRight',
        bubbles: true,
        cancelable: true,
      }));
      await waitFor(
        () => data.mainTab === 'file',
        '切换到文件标签',
      );
      const fileTab = document.querySelector(
        '.main-tab[data-id="file:a.py"]',
      ) as HTMLElement;
      assert.ok(fileTab);
      assert.equal(document.activeElement, fileTab);

      fileTab.dispatchEvent(new window.KeyboardEvent('keydown', {
        key: 'Delete',
        bubbles: true,
        cancelable: true,
      }));
      await waitFor(
        () => data.editorTabs.length === 0 && data.mainTab === 'chat',
        '删除标签',
      );
    });

    await t.test('图片文件显示预览而非文本', async () => {
      await data.openFile('logo.png', 0, true);
      await waitFor(
        () => data.mainTab === 'file' && data.fileView._isImage === true,
        '图片标签',
      );
      const image = document.querySelector(
        '.image-preview img',
      ) as HTMLImageElement;
      assert.ok(image);
      assert.equal(
        image.getAttribute('src'),
        'data:image/png;base64,iVBORw0KGgo=',
      );
      await waitFor(
        () => (document.querySelector('.editor-code-wrap') as HTMLElement)
          .style.display === 'none',
        '隐藏文本区',
      );
      data.closeActiveEditorTab();
    });

    await t.test('会话可打开实时分屏、接收事件并独立发送', async () => {
      assert.ok(
        document.querySelector('[data-action="session"][data-op="split"]'),
      );
      data.openSplitSession('thread-dom-other');
      await waitFor(
        () => data.splitOpen && data.splitLoading === false,
        '分屏加载',
      );
      assert.equal(data.splitTitle, '普通会话');
      const pane = document.querySelector('.split-pane') as HTMLElement;
      await waitFor(
        () => (pane.textContent || '').includes('分屏回答'),
        '分屏消息',
      );
      assert.equal(data.splitView.messages.length, 2);

      const frame = {
        type: 'assistant_token',
        node: 'split-node',
        text: '分屏实时消息',
      };
      await data.consume(
        new Response(`id: 1\ndata: ${JSON.stringify(frame)}\n\n`, { status: 200 }),
        data.splitView,
      );
      await waitFor(
        () => (pane.textContent || '').includes('分屏实时消息'),
        '分屏实时更新',
      );

      assert.ok(pane.classList.contains('split-pane-active'));

      const mainEl = document.querySelector('.main') as HTMLElement;
      const originalRect = mainEl.getBoundingClientRect.bind(mainEl);
      mainEl.getBoundingClientRect = () => ({
        left: 0, top: 0, right: 1000, bottom: 900,
        width: 1000, height: 900, x: 0, y: 0,
        toJSON() { return {}; },
      } as DOMRect);
      const splitter = document.querySelector('.splitter-split') as HTMLElement;
      splitter.dispatchEvent(new window.MouseEvent('pointerdown', {
        clientX: 500,
        bubbles: true,
        cancelable: true,
      }));
      window.dispatchEvent(new window.MouseEvent('pointermove', {
        clientX: 600,
      }));
      assert.equal(data.splitWidth, 400);
      window.dispatchEvent(new window.MouseEvent('pointerup'));
      const layout = JSON.parse(
        window.localStorage.getItem('codingagent.layout') || '{}',
      );
      assert.equal(layout.splitWidth, 400);
      mainEl.getBoundingClientRect = originalRect;
      data.resetLayout();

      data.focusPaneMain();
      assert.equal(data.activePane, 'main');
      data.focusPaneSplit();
      assert.equal(data.activePane, 'split');

      data.splitInput = '分屏任务';
      await data.sendSplit();
      await waitFor(
        () => data.splitView.messages.some((m) => m.text === '分屏任务'),
        '分屏发送',
      );
      assert.equal(
        data.messages.some((m) => m.text === '分屏任务'),
        false,
      );

      data.closeSplit();
      await waitFor(() => pane.style.display === 'none', '分屏关闭');
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
      await waitFor(
        () => document.querySelector('.code-block .code-lang')?.textContent === 'js',
        '代码块标题',
      );
      assert.ok(document.querySelector('.code-block .copy-code'));
      assert.equal(
        document.querySelector('.code-block .code-lines')?.textContent?.trim(),
        '1 行',
      );
    });

    await t.test('Toast 堆叠与语义色', async () => {
      data.toasts = [];
      data.toastMessage('已保存');
      data.toastMessage('保存失败');
      await waitFor(
        () => document.querySelectorAll('.toast').length === 2,
        'toast 堆叠',
      );
      assert.equal(data.toasts[0]._cls, 'toast toast-success');
      assert.equal(data.toasts[1]._cls, 'toast toast-error');
      assert.equal(
        document.querySelector('.toast-error .icon use')?.getAttribute('href'),
        '#i-alert',
      );
      data.toasts = [];
    });

    await t.test('大工具输出分页渲染且派生列表复用缓存', async () => {
      const preview = Array.from(
        { length: 300 },
        (_, index) => `line ${index}`,
      ).join('\n');
      data.handleEvent({
        type: 'tool_call_started',
        call_id: 'big',
        name: 'run',
        summary: 'big output',
        level: '执行',
      });
      data.handleEvent({
        type: 'tool_call_finished',
        call_id: 'big',
        name: 'run',
        ok: true,
        preview,
      });
      const message = data.messages.find((m) => m.call_id === 'big');
      assert.ok(message);
      assert.equal(message._outputText.split('\n').length, 40);
      assert.equal(message._outputMore, true);
      assert.match(message._outputMoreLabel, /剩余 260 行/);

      const first = data.filteredTools();
      const second = data.filteredTools();
      assert.equal(first, second);
      data.setToolFilter('ok');
      assert.notEqual(data.filteredTools(), first);
      data.setToolFilter('all');

      data.moreToolOutput(message);
      assert.equal(message._outputText.split('\n').length, 240);
      assert.match(message._outputMoreLabel, /剩余 60 行/);

      await waitFor(
        () => Boolean(document.querySelector('.inline-tool-out')),
        '工具输出渲染',
      );
      const pre = document.querySelector('.inline-tool-out') as HTMLElement;
      assert.equal(pre.textContent.trim().split('\n').length, 240);
    });

    await t.test('阶段变化写入独立播报区而非 transcript', async () => {
      const transcript = document.querySelector('.transcript') as HTMLElement;
      assert.equal(transcript.getAttribute('aria-live'), 'off');
      data.handleEvent({ type: 'run_started' });
      await waitFor(
        () => document.querySelector('.live-region')?.textContent?.trim() === '开始运行',
        '开始运行播报',
      );
      data.handleEvent({ type: 'plan_created', steps: ['a', 'b'] });
      await waitFor(
        () => document.querySelector('.live-region')?.textContent?.includes('计划已生成：2 步'),
        '计划播报',
      );
    });

    await t.test('主题设置应用高对比度/强调色/代码排版', async () => {
      data.contrast = 'high';
      data.accent = 'violet';
      data.codeFontSize = 'large';
      data.codeLineHeight = 'relaxed';
      data.saveSettings();
      await waitFor(
        () => document.body.classList.contains('contrast-high')
          && document.body.classList.contains('code-large')
          && document.body.classList.contains('code-lh-relaxed'),
        '主题 class',
      );
      assert.equal(
        document.body.style.getPropertyValue('--accent'),
        '#a78bfa',
      );
      const saved = JSON.parse(
        window.localStorage.getItem('codingagent.settings') || '{}',
      );
      assert.equal(saved.accent, 'violet');
      assert.equal(saved.codeFontSize, 'large');
      assert.equal(saved.codeLineHeight, 'relaxed');

      data.contrast = 'normal';
      data.accent = 'blue';
      data.codeFontSize = 'normal';
      data.codeLineHeight = 'normal';
      data.saveSettings();
      await waitFor(
        () => !document.body.classList.contains('contrast-high')
          && document.body.classList.contains('code-normal'),
        '主题恢复',
      );
      assert.equal(
        document.body.style.getPropertyValue('--accent'),
        '#5b9dff',
      );
    });

    await t.test('设置搜索按关键词过滤分节', async () => {
      const opener = document.querySelector(
        'header .icon-btn[aria-label="设置"]',
      ) as HTMLElement;
      opener.focus();
      opener.click();
      const modal = document.querySelector('.settings-modal') as HTMLElement;
      await waitFor(() => modal.style.display !== 'none', '设置打开');
      const sections = () => Array.from(
        document.querySelectorAll('.settings-view'),
      ) as HTMLElement[];
      data.settingsQuery = '模型';
      await waitFor(
        () => sections()[1].style.display !== 'none'
          && sections()[0].style.display === 'none',
        '搜索模型',
      );
      assert.equal(data.settingsSearchLabel, '找到 1 个分类');

      data.settingsQuery = '不存在的设置';
      await waitFor(() => data.settingsNoResults === true, '无搜索结果');
      data.settingsQuery = '';
      await waitFor(() => sections()[0].style.display !== 'none', '清空搜索');
      keydown(window, { key: 'Escape' });
      await waitFor(() => modal.style.display === 'none', '设置关闭');
    });

    await t.test('加载骨架与全局错误横幅可关闭/重试', async () => {
      data.sessions = [];
      data.sessionsLoading = true;
      await waitFor(
        () => document.querySelector('.skeleton-list')?.style.display !== 'none',
        '会话骨架',
      );
      data.sessionsLoading = false;

      data.fileLoading = true;
      await waitFor(
        () => document.querySelector('.editor-skeleton')?.style.display !== 'none',
        '文件骨架',
      );
      data.fileLoading = false;

      data.reportError('测试错误', 'sessions');
      const banner = document.querySelector('.error-banner') as HTMLElement;
      await waitFor(() => banner.style.display !== 'none', '错误横幅');
      assert.match(banner.textContent || '', /测试错误/);
      data.dismissError();
      await waitFor(() => banner.style.display === 'none', '关闭横幅');

      data.reportError('会话加载失败', 'sessions');
      await waitFor(() => banner.style.display !== 'none', '错误横幅重试');
      data.retryLastError();
      await waitFor(() => banner.style.display === 'none', '重试后关闭');
      await waitFor(() => data.sessions.length === 2, '重试加载会话');
    });

    await t.test('设置模态焦点进入、Tab 循环、Esc 关闭并恢复', async () => {
      const opener = document.querySelector(
        'header .icon-btn[aria-label="设置"]',
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

    await t.test('applyEvent 按会话视图隔离状态', async () => {
      const original = data.activeView;
      const viewA = data.createSessionView('thread-a');
      const viewB = data.createSessionView('thread-b');

      data.applyEvent(viewA, { type: 'run_started' });
      data.applyEvent(viewA, {
        type: 'plan_created',
        steps: ['A1', 'A2'],
        kinds: ['x', 'y'],
      });
      data.applyEvent(viewA, {
        type: 'tool_call_started',
        call_id: 'same',
        name: 'tool',
        summary: 'A tool',
      });
      data.applyEvent(viewA, {
        type: 'tool_call_finished',
        call_id: 'same',
        name: 'tool',
        ok: true,
        preview: 'A out',
      });
      data.applyEvent(viewA, {
        type: 'assistant_token',
        node: 'n',
        text: 'A text',
      });
      data.applyEvent(viewA, {
        type: 'verification',
        ok: false,
        summary: 'A verify',
        issues: ['a.py:1'],
      });

      data.applyEvent(viewB, { type: 'run_started' });
      data.applyEvent(viewB, {
        type: 'plan_created',
        steps: ['B1'],
        kinds: ['z'],
      });
      data.applyEvent(viewB, {
        type: 'tool_call_started',
        call_id: 'same',
        name: 'tool',
        summary: 'B tool',
      });
      data.applyEvent(viewB, {
        type: 'tool_call_finished',
        call_id: 'same',
        name: 'tool',
        ok: false,
        preview: 'B out',
      });
      data.applyEvent(viewB, {
        type: 'assistant_token',
        node: 'n',
        text: 'B text',
      });

      assert.equal(data.activeView, original);
      assert.deepEqual(viewA.plan, ['A1', 'A2']);
      assert.deepEqual(viewB.plan, ['B1']);
      assert.equal(viewA.tools.length, 1);
      assert.equal(viewB.tools.length, 1);
      assert.equal(viewA.tools[0].summary, 'A tool');
      assert.equal(viewB.tools[0].summary, 'B tool');
      assert.equal(viewA.tools[0].state, 'ok');
      assert.equal(viewB.tools[0].state, 'error');
      assert.equal(viewA.problems.length, 1);
      assert.equal(viewB.problems.length, 0);
      assert.notEqual(viewA.messages[0].id, viewB.messages[0].id);

      await waitFor(
        () => viewA.messages.some((m) => m.text === 'A text')
          && viewB.messages.some((m) => m.text === 'B text'),
        '会话 token 渲染',
      );
    });

    await t.test('审批弹窗批准/全部拒绝后关闭', async () => {
      data.handleEvent({
        type: 'approval_requested',
        request_id: 'req-1',
        tool: 'run',
        command: 'rm -rf /tmp/demo',
        level: '危险',
        reason: '需要人工确认',
      });
      const modal = document.querySelector(
        '.modal[aria-labelledby="approval-title"]',
      ) as HTMLElement;
      await waitFor(() => modal.style.display !== 'none', '审批弹窗');
      assert.match(
        modal.querySelector('pre')?.textContent || '',
        /rm -rf/,
      );

      const approve = modal.querySelector(
        '[data-action="approval"][data-allow="true"]',
      ) as HTMLElement;
      approve.click();
      await waitFor(
        () => data.approvals.length === 0
          && modal.style.display === 'none'
          && data.busy === false,
        '批准后关闭',
      );

      data.handleEvent({
        type: 'approval_requested',
        request_id: 'req-2',
        tool: 'run',
        command: 'ls',
        level: '写',
      });
      data.handleEvent({
        type: 'approval_requested',
        request_id: 'req-3',
        tool: 'run',
        command: 'cat a.txt',
        level: '读',
      });
      await waitFor(() => data.approvals.length === 2, '两条审批');
      const rejectAll = Array.from(
        modal.querySelectorAll('button'),
      ).find((button) => button.textContent?.includes('全部拒绝')) as HTMLElement;
      rejectAll.click();
      await waitFor(
        () => data.approvals.length === 0
          && modal.style.display === 'none'
          && data.busy === false,
        '全部拒绝后关闭',
      );
    });

    await t.test('侧栏与检查器可拖拽并持久化宽度', async () => {
      data.sidebarCollapsed = false;
      data.inspectorCollapsed = false;
      const splitter = document.querySelector('.splitter-left') as HTMLElement;
      assert.ok(splitter);
      splitter.dispatchEvent(new window.MouseEvent('pointerdown', {
        clientX: 280,
        bubbles: true,
        cancelable: true,
      }));
      window.dispatchEvent(new window.MouseEvent('pointermove', {
        clientX: 330,
      }));
      assert.equal(data.sidebarWidth, 278);
      window.dispatchEvent(new window.MouseEvent('pointerup'));
      const saved = JSON.parse(
        window.localStorage.getItem('codingagent.layout') || '{}',
      );
      assert.equal(saved.sidebarWidth, 278);

      const inspectorSplitter = document.querySelector(
        '.splitter-right',
      ) as HTMLElement;
      assert.ok(inspectorSplitter);
      inspectorSplitter.dispatchEvent(new window.MouseEvent('pointerdown', {
        clientX: 700,
        bubbles: true,
        cancelable: true,
      }));
      window.dispatchEvent(new window.MouseEvent('pointermove', {
        clientX: 650,
      }));
      assert.equal(data.inspectorWidth, 374);
      window.dispatchEvent(new window.MouseEvent('pointerup'));
      data.resetLayout();
    });

    await t.test('切换 activeView 后模板与状态跟随新会话', async () => {
      const previous = data.activeView;
      data.activateSessionView('thread-switched');
      data.pushMessage('assistant', '视图切换消息', true, 'final');
      await waitFor(
        () => (document.querySelector('.transcript')?.textContent || '')
          .includes('视图切换消息'),
        '新视图渲染',
      );
      assert.notEqual(data.activeView, previous);
      assert.equal(data.messages.length, 1);
      assert.equal(previous.messages.length > 1, true);
    });

    await t.test('SessionStore 复用视图并保留会话状态', () => {
      data.resetSessionViews();
      data.activateSessionView('store-a', true);
      data.pushMessage('assistant', 'A 状态', true, 'final');
      data.approvals = [{
        request_id: 'store-req',
        command: 'ls',
        level: '读',
      }];
      data.showNextApproval();

      data.activateSessionView('store-b', true);
      data.pushMessage('assistant', 'B 状态', true, 'final');

      const again = data.activateSessionView('store-a');
      assert.equal(again, data.activeView);
      assert.equal(data.messages.length, 1);
      assert.equal(data.messages[0].text, 'A 状态');
      assert.equal(data.approvals.length, 1);
      assert.equal(data.approval.request_id, 'store-req');

      data.disposeSessionView('store-a');
      assert.equal(
        Object.prototype.hasOwnProperty.call(data.sessionViews, 'store-a'),
        false,
      );
      assert.equal(data.threadId, '');
    });

    await t.test('SessionStore 限制保留的视图数量', () => {
      data.resetSessionViews();
      for (let index = 0; index < 9; index += 1) {
        data.activateSessionView(`cap-${index}`, true);
      }
      assert.equal(Object.keys(data.sessionViews).length <= 6, true);
      assert.equal(data.sessionViews['cap-8'] !== undefined, true);
    });

    await t.test('重复打开会话不重置已缓存视图', async () => {
      data.resetSessionViews();
      await data.openSession('thread-dom-test');
      await waitFor(
        () => data.messages.length === 2
          && data.messages[1].text === '历史回答',
        '首次加载历史',
      );
      data.pushMessage('assistant', '临时状态', true, 'final');

      await data.openSession('thread-dom-other');
      await waitFor(() => data.threadId === 'thread-dom-other', '切换到 B');
      await data.openSession('thread-dom-test');
      await waitFor(() => data.threadId === 'thread-dom-test', '切回 A');

      assert.equal(data.messages.length, 3);
      assert.equal(data.messages[2].text, '临时状态');
      assert.equal(data.sessionViews['thread-dom-test'].loaded, true);
    });

    await t.test('忙时仍可切换，旧视图保持运行状态', async () => {
      data.activateSessionView('thread-dom-test');
      data.busy = true;
      await data.openSession('thread-dom-other');
      assert.equal(data.threadId, 'thread-dom-other');
      assert.equal(data.busy, false);
      const previous = data.sessionViews['thread-dom-test'];
      assert.equal(previous.busy, true);
      previous.busy = false;
    });

    await t.test('后台 SSE 事件写入所属视图', async () => {
      data.resetSessionViews();
      const active = data.activateSessionView('stream-active', true);
      const background = data.activateSessionView('stream-background', true);
      data.activateSessionView('stream-active');

      const frames = [
        { type: 'assistant_token', node: 'n', text: '后台输出' },
        { type: 'plan_created', steps: ['后台计划'], kinds: ['x'] },
      ];
      const body = frames
        .map((frame, index) => (
          `id: ${index + 1}\ndata: ${JSON.stringify(frame)}\n\n`
        ))
        .join('');
      await data.consume(new Response(body, { status: 200 }), background);

      assert.equal(data.activeView, active);
      assert.equal(background.plan[0], '后台计划');
      await waitFor(
        () => background.messages.some((m) => m.text === '后台输出'),
        '后台视图 token',
      );
      assert.equal(active.messages.length, 0);
    });

    await t.test('后台完成会标记未读且不打断当前会话', async () => {
      data.resetSessionViews();
      data.activateSessionView('thread-dom-test');
      const background = data.activateSessionView('thread-dom-other');
      data.activateSessionView('thread-dom-test');

      data.applyEvent(background, {
        type: 'run_finished',
        answer: '后台完成',
      });
      await waitFor(
        () => {
          const row = data.sessions.find(
            (item) => item.thread_id === 'thread-dom-other',
          );
          return Boolean(row && row.unread);
        },
        '后台未读',
      );
      assert.equal(data.threadId, 'thread-dom-test');
      assert.equal(
        data.messages.some((m) => m.text === '后台完成'),
        false,
      );
    });

    await t.test('输入草稿按会话保存并在切换后恢复', async () => {
      data.resetSessionViews();
      window.localStorage.removeItem('codingagent.drafts');
      data.activateSessionView('thread-dom-test');
      const textarea = document.querySelector(
        '.composer textarea',
      ) as HTMLTextAreaElement;
      textarea.value = '未发送的草稿';
      textarea.dispatchEvent(new window.Event('input', { bubbles: true }));
      await new Promise((resolve) => setTimeout(resolve, 500));

      const stored = JSON.parse(
        window.localStorage.getItem('codingagent.drafts') || '{}',
      );
      assert.equal(stored['thread-dom-test'], '未发送的草稿');

      await data.openSession('thread-dom-other');
      await waitFor(() => data.threadId === 'thread-dom-other', '切到会话 B');
      assert.equal(data.prompt, '');
      await data.openSession('thread-dom-test');
      await waitFor(() => data.threadId === 'thread-dom-test', '切回会话 A');
      assert.equal(data.prompt, '未发送的草稿');

      data.clearDraftFor('thread-dom-test');
      data.prompt = '';
    });

    await t.test('URL 深链恢复会话并随切换同步', async () => {
      data.resetSessionViews();
      window.history.replaceState(null, '', '/?session=thread-dom-other');
      await data.applyUrlState();
      await waitFor(() => data.threadId === 'thread-dom-other', '深链恢复');
      assert.match(window.location.search, /session=thread-dom-other/);

      await data.openSession('thread-dom-test');
      await waitFor(() => data.threadId === 'thread-dom-test', '切回测试会话');
      assert.match(window.location.search, /session=thread-dom-test/);

      await data.openSplitSession('thread-dom-other');
      assert.match(window.location.search, /split=thread-dom-other/);
      data.closeSplit();
      assert.doesNotMatch(window.location.search, /split=/);

      await data.newSession();
      assert.doesNotMatch(window.location.search, /session=/);
    });

    await t.test('系统通知只在页面隐藏且已授权时触发', () => {
      const calls: Array<{ title: string; options: any }> = [];
      const originalNotification = (window as any).Notification;
      (window as any).Notification = class {
        static permission = 'granted';
        constructor(title: string, options: any) {
          calls.push({ title, options });
        }
      };
      const descriptor = Object.getOwnPropertyDescriptor(
        document,
        'visibilityState',
      );
      Object.defineProperty(document, 'visibilityState', {
        configurable: true,
        get: () => 'hidden',
      });

      data.notifyEnabled = true;
      data.notify('CodingAgent 运行完成', '答案', {
        sessionId: 'thread-dom-test',
      });
      assert.equal(calls.length, 1);
      assert.equal(calls[0].title, 'CodingAgent 运行完成');

      Object.defineProperty(document, 'visibilityState', {
        configurable: true,
        get: () => 'visible',
      });
      data.notify('CodingAgent 运行失败', '错误');
      assert.equal(calls.length, 1, '页面可见时不重复弹系统通知');

      data.notifyEnabled = false;
      if (originalNotification === undefined) {
        delete (window as any).Notification;
      } else {
        (window as any).Notification = originalNotification;
      }
      if (descriptor) {
        Object.defineProperty(document, 'visibilityState', descriptor);
      } else {
        delete (document as any).visibilityState;
      }
    });

    await t.test('语言切换更新壳层文案并持久化', async () => {
      data.setView('sessions');
      data.changeLocale({ target: { value: 'en-US' } });
      await waitFor(
        () => document.querySelector('header button[aria-label="Settings"]') !== null,
        '英文壳层',
      );
      assert.equal(document.documentElement.lang, 'en-US');
      assert.equal(
        document.querySelector('.sidebar-head h3')?.textContent?.trim(),
        'Sessions',
      );
      assert.equal(
        document.querySelector('#settings-title')?.textContent?.trim(),
        'Settings',
      );
      const saved = JSON.parse(
        window.localStorage.getItem('codingagent.settings') || '{}',
      );
      assert.equal(saved.locale, 'en-US');

      data.changeLocale({ target: { value: 'zh-CN' } });
      await waitFor(
        () => document.querySelector('header button[aria-label="设置"]') !== null,
        '中文壳层',
      );
      assert.equal(document.documentElement.lang, 'zh-CN');
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
