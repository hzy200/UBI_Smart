/* Alpine mixin: transcript。由 app.ts 拆分，方法名保持稳定以兼容模板与构建守卫。 */

import {
  renderMarkdown,
} from '../ui/format';
import { renderMarkdownIncremental } from '../ui/markdown';
import { outputPage } from '../ui/output';
import { memoize } from '../ui/memo';
import {
  markdownCaches,
  messageHeights,
  renderTimers,
  tokenBuffers,
  virtualMessagesSlot,
} from '../state/runtime';
import { includesQuery } from '../ui/search';
import { computeVirtualRange } from '../ui/virtual';

const VIRTUAL_THRESHOLD = 200;
const MESSAGE_GAP = 14;
const OVERSCAN = 6;

export const transcriptMixin = {
  pushMessage(role, text, done = true, phase = '') {
    if (role === 'assistant') {
      const last = this.messages[this.messages.length - 1];
      if (
        last &&
        last.role === 'assistant' &&
        last.kind !== 'tool' &&
        (last.text || '').trim() === String(text || '').trim()
      ) {
        return last;
      }
    }
    const resolvedPhase = phase || (
      role === 'assistant' ? 'answer' : role
    );
    const message = {
      id: ++this._msgSeq,
      role,
      phase: resolvedPhase,
      text,
      html: renderMarkdown(text),
      done,
      _expanded: false,
      _cls: `msg-${role}${role === 'assistant' ? ' msg-answer' : ''}`,
    };
    this.decorateMessage(message, resolvedPhase);
    this.messages.push(message);
    if (!this._isBackgroundApply) {
      this.scrollBottom(role === 'user' || resolvedPhase === 'final');
      this.scheduleVirtualRange();
      this.$nextTick(() => this.decorateTranscript());
    }
    return message;
  },

  decorateMessage(message, phase) {
    const resolved = phase || message.phase || message.role;
    message._isUser = message.role === 'user';
    message._isTool = message.kind === 'tool';
    message._notTool = !message._isTool;
    message._isFinal = message.role === 'assistant' && resolved === 'final';
    message._label = message._isTool
      ? '工具'
      : message.role === 'user'
        ? '你'
        : message.role === 'system'
          ? '过程'
          : message.role === 'assistant'
            ? (resolved === 'final'
              ? '最终回答'
              : resolved === 'process' ? '工作中' : '回答')
            : message.role || '';
    const text = String(message.text || '');
    message._longAnswer = !message._isTool
      && (text.length > 1200 || text.split('\n').length > 24);
    message._hasToolText = Boolean(message.summary || message.preview);
    if (message._isTool) {
      const page = outputPage(
        message.preview || '',
        message._outputLimit || 40,
      );
      message._outputText = page.text;
      message._outputHint = page.hint;
      message._outputHidden = page.hidden;
      message._outputMore = page.truncated;
      message._outputMoreLabel = page.truncated
        ? `加载更多（剩余 ${page.hidden} 行）`
        : '';
    }
    message._badgeClass = `badge-${resolved}`;
    message._hit = Boolean(message._hit);
    message._renderClass = `${message._cls || ''}${message._hit ? ' msg-hit' : ''}`;
    message._timing = message._timing || '';
    message._longOutput = Boolean(message._longOutput);
    message._dotClass = (message._cls || '').includes('tool-ok')
      ? 'dot-ok'
      : ((message._cls || '').includes('tool-error')
        || (message._cls || '').includes('tool-rejected'))
        ? 'dot-bad'
        : 'dot-run';
    message._outputClass = (!message._expanded && message._longOutput)
      ? 'is-collapsed'
      : '';
    message._answerClass = (!message._expanded && message._longAnswer)
      ? 'msg-collapsed'
      : '';
    return message;
  },

  messageLabel(m) {
    if (!m) return '';
    if (m.kind === 'tool') return '工具';
    if (m.role === 'user') return '你';
    if (m.role === 'system') return '过程';
    if (m.role === 'assistant') {
      if (m.phase === 'final') return '最终回答';
      if (m.phase === 'process') return '工作中';
      return '回答';
    }
    return m.role || '';
  },

  markFinalAnswer() {
    for (let index = this.messages.length - 1; index >= 0; index -= 1) {
      const message = this.messages[index];
      if (message.role === 'assistant' && message.kind !== 'tool') {
        message.phase = 'final';
        message.done = true;
        message._cls = 'msg-assistant msg-final';
        this.decorateMessage(message, 'final');
        return message;
      }
    }
    return null;
  },

  moveFinalAnswerLast() {
    // 运行尾部可能还有上下文裁剪等过程提示；最终回答必须显示在最后。
    const index = this.messages.findIndex(
      (message) =>
        message.role === 'assistant' &&
        message.kind !== 'tool' &&
        message.phase === 'final',
    );
    if (index < 0 || index === this.messages.length - 1) return;
    const finalMessage = this.messages[index];
    this.messages = [
      ...this.messages.slice(0, index),
      ...this.messages.slice(index + 1),
      finalMessage,
    ];
  },

  isAnswerLong(m) {
    if (!m || m.kind === 'tool' || !m.done) return false;
    const text = String(m.text || '');
    return text.length > 1200 || text.split('\n').length > 24;
  },

  async copyTranscriptMessage(m) {
    const text = m && m.kind === 'tool'
      ? [m.summary, m.preview].filter(Boolean).join('\n')
      : String((m && m.text) || '');
    await this.copyText(text);
  },

  dedupeAssistantMessages() {
    const kept = [];
    let lastAssistant = null;
    for (const message of this.messages) {
      if (message.role === 'assistant' && message.kind !== 'tool') {
        const text = String(message.text || '').trim();
        const previous = String(
          (lastAssistant && lastAssistant.text) || '',
        ).trim();
        if (text && previous && text === previous) {
          // 跨工具卡片/系统提示重复的同一段回答也只保留一次。
          continue;
        }
        lastAssistant = message;
      }
      kept.push(message);
    }
    this.messages = kept;
  },

  decorateTranscript() {
    const root = this.$refs.transcript;
    if (!root) return;
    root.querySelectorAll('.msg-body pre').forEach((pre) => {
      const code = pre.querySelector('code');
      if (window.hljs && code && !code.dataset.highlighted) {
        try { window.hljs.highlightElement(code); } catch (e) { /* ignore */ }
      }
      if (pre.closest('.code-block')) return;
      const language = (
        (code && code.className.match(/language-([\w+-]+)/)) || []
      )[1] || 'text';
      const source = code ? code.textContent || '' : pre.textContent || '';
      const lineCount = source.replace(/\n$/, '').split('\n').length;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'copy-code';
      button.textContent = '复制';
      button.setAttribute('aria-label', '复制代码块');
      button.addEventListener('click', () => this.copyCode(pre));
      const wrapper = document.createElement('div');
      wrapper.className = 'code-block';
      const head = document.createElement('div');
      head.className = 'code-block-head';
      const lang = document.createElement('span');
      lang.className = 'code-lang';
      lang.textContent = language;
      const lines = document.createElement('span');
      lines.className = 'code-lines';
      lines.textContent = `${lineCount} 行`;
      head.append(lang, lines, button);
      pre.parentNode?.insertBefore(wrapper, pre);
      wrapper.append(head, pre);
    });
    if (!this.virtualEnabled() || !window.ResizeObserver) return;
    const key = '__codingAgentMessageObserver';
    if (!(root as any)[key]) {
      (root as any)[key] = new ResizeObserver((entries) => {
        for (const entry of entries) {
          const element = entry.target as HTMLElement;
          const id = Number(element.getAttribute('data-message-id'));
          if (!id) continue;
          const oldHeight = messageHeights.get(id) || 0;
          const height = element.getBoundingClientRect().height + MESSAGE_GAP;
          if (oldHeight && Math.abs(oldHeight - height) < 1) continue;
          messageHeights.set(id, height);
          this.onMessageMeasured(id, oldHeight, height);
        }
      });
    }
    const observer = (root as any)[key] as ResizeObserver;
    observer.disconnect();
    root.querySelectorAll('.msg[data-message-id]').forEach((element) => {
      observer.observe(element);
    });
  },

  async copyText(text) {
    const value = String(text || '');
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      this.toastMessage('已复制');
      return;
    } catch (err) {
      // WebView2 在部分策略下拒绝 Clipboard API，退回到选区复制。
    }
    const area = document.createElement('textarea');
    area.value = value;
    area.style.position = 'fixed';
    area.style.left = '-9999px';
    area.style.opacity = '0';
    document.body.appendChild(area);
    try {
      area.focus();
      area.select();
      const ok = document.execCommand('copy');
      this.toastMessage(ok ? '已复制' : '复制失败：当前环境不支持剪贴板');
    } catch (err) {
      this.toastMessage('复制失败：当前环境不支持剪贴板');
    } finally {
      area.remove();
    }
  },

  async copyCode(pre) {
    const code = pre.querySelector('code');
    await this.copyText((code ? code.textContent : pre.textContent) || '');
  },

  announce(text) {
    if (this._isBackgroundApply) return;
    const value = String(text || '').replace(/\s+/g, ' ').trim();
    if (!value) return;
    this.liveAnnouncement = value;
  },

  appendAssistantToken(node, text) {
    if (!this._currentMessage || this._currentNode !== node) {
      if (this._currentMessage) {
        this._currentMessage.done = true;
      }
      this._currentNode = node;
      this._currentMessage = {
        id: ++this._msgSeq,
        role: 'assistant',
        phase: 'process',
        text: '',
        html: '',
        done: false,
        _expanded: false,
        _cls: 'msg-assistant msg-process',
      };
      this.decorateMessage(this._currentMessage, 'process');
      this.messages.push(this._currentMessage);
      tokenBuffers.set(this._currentMessage.id, '');
    }
    const id = this._currentMessage.id;
    // 每个 token 只追加到模块级缓冲，避免逐 token 触发 Alpine 响应式
    // 更新和整段 Markdown 重解析。用节流而不是防抖：token 连续到达时
    // 仍然每 60ms 刷新一次，而不是等流结束才一次性出现。
    tokenBuffers.set(id, (tokenBuffers.get(id) || '') + text);
    // 定时器属于会话视图：applyEvent 只临时切换 activeView，异步回调不能
    // 再依赖它。阶段 3 的 SessionStore 会把这个 view 固定到视图实例上。
    const view = this.activeView;
    if (renderTimers.has(id)) return;
    renderTimers.set(id, setTimeout(() => {
      renderTimers.delete(id);
      const target = view.messages.find((item) => item.id === id);
      const raw = tokenBuffers.get(id) || '';
      if (target) {
        let cache = markdownCaches.get(id);
        if (!cache) {
          cache = { head: '', html: '' };
          markdownCaches.set(id, cache);
        }
        target.text = raw;
        target.html = renderMarkdownIncremental(raw, cache, renderMarkdown);
        this.decorateMessage(target);
      }
      if (view === this.activeView) this.scrollBottom(false);
    }, 60));
  },

  flushAssistantToken(id) {
    const timer = renderTimers.get(id);
    if (timer !== undefined) {
      clearTimeout(timer);
      renderTimers.delete(id);
    }
    const target = this.messages.find((item) => item.id === id);
    const raw = tokenBuffers.get(id);
    if (target && raw !== undefined) {
      const cache = markdownCaches.get(id) || { head: '', html: '' };
      target.text = raw;
      target.html = renderMarkdownIncremental(raw, cache, renderMarkdown);
      this.decorateMessage(target);
    }
    markdownCaches.delete(id);
    tokenBuffers.delete(id);
  },

  finishAssistant(view = this.activeView) {
    return this.withView(view, () => {
      if (this._currentMessage) {
        this.flushAssistantToken(this._currentMessage.id);
        this._currentMessage.done = true;
        this.decorateMessage(this._currentMessage);
      }
      this._currentMessage = null;
      this._currentNode = '';
      if (!this._isBackgroundApply) {
        this.$nextTick(() => this.decorateTranscript());
      }
    });
  },

  virtualEnabled() {
    return this.messages.length > VIRTUAL_THRESHOLD;
  },

  transcriptClass() {
    return this.virtualEnabled() ? 'is-virtual' : '';
  },

  estimateMessageHeight(message) {
    if (!message) return 80;
    if (message.kind === 'tool') return 90;
    if (message.role === 'system') return 40;
    if (message.role === 'user') return 72;
    return 120;
  },

  virtualHeights() {
    return this.messages.map((message) => (
      (messageHeights.get(message.id) || this.estimateMessageHeight(message))
      + MESSAGE_GAP
    ));
  },

  virtualMessages() {
    const key = [
      this.threadId,
      this._msgSeq,
      this.virtualStart,
      this.virtualEnd,
      this.virtualTop,
      this.messages.length,
    ].join(':');
    return memoize(virtualMessagesSlot, key, () => {
      if (!this.virtualEnabled()) return this.messages;
      if (!this.virtualEnd) {
        return this.messages.slice(Math.max(0, this.messages.length - 80));
      }
      return this.messages.slice(this.virtualStart, this.virtualEnd);
    });
  },

  scheduleVirtualRange() {
    if (this._isBackgroundApply) return;
    if (!this.virtualEnabled() || this._virtualRaf) return;
    this._virtualRaf = requestAnimationFrame(() => {
      this._virtualRaf = 0;
      this.recomputeVirtualRange();
    });
  },

  recomputeVirtualRange() {
    const root = this.$refs.transcript as HTMLElement | null;
    if (!root || !this.virtualEnabled()) {
      this.virtualStart = 0;
      this.virtualEnd = this.messages.length;
      this.virtualTop = 0;
      this.virtualBottom = 0;
      return;
    }
    const range = computeVirtualRange(
      this.virtualHeights(),
      root.scrollTop,
      root.clientHeight,
      OVERSCAN,
    );
    const changed = (
      this.virtualStart !== range.start || this.virtualEnd !== range.end
    );
    this.virtualStart = range.start;
    this.virtualEnd = range.end;
    this.virtualTop = range.top;
    this.virtualBottom = range.bottom;
    if (changed) this.$nextTick(() => this.decorateTranscript());
  },

  onMessageMeasured(id, oldHeight, newHeight) {
    if (!this.virtualEnabled()) return;
    const index = this.messages.findIndex((message) => message.id === id);
    if (index >= 0 && index < this.virtualStart && oldHeight > 0) {
      const root = this.$refs.transcript as HTMLElement | null;
      if (root) root.scrollTop += newHeight - oldHeight;
    }
    this.scheduleVirtualRange();
  },

  scrollBottom(force = true) {
    if (this._isBackgroundApply) return;
    this.$nextTick(() => {
      const element = this.$refs.transcript;
      if (!element) return;
      if (!force && this.showJumpLatest) return;
      const apply = () => {
        element.scrollTop = element.scrollHeight;
        this.showJumpLatest = false;
      };
      apply();
      this.recomputeVirtualRange();
      this.$nextTick(apply);
    });
  },

  onTranscriptScroll(event) {
    const element = event && event.target;
    if (!element) return;
    const remaining = element.scrollHeight - element.scrollTop - element.clientHeight;
    this.showJumpLatest = remaining > 120;
    this.scheduleVirtualRange();
  },

  messageClass(message) {
    const hit = this.messageMatches.includes(message.id) ? ' msg-hit' : '';
    return `${message._cls || ''}${hit}`;
  },

  editMessage(message) {
    if (!message || message.role !== 'user') return;
    this.prompt = String(message.text || '');
    this.closeMessageSearch();
    this.$nextTick(() => {
      const composer = document.querySelector('.composer textarea') as HTMLTextAreaElement | null;
      if (composer) composer.focus();
    });
    this.toastMessage('已填入输入框；发送后会作为新一轮');
  },

  regenerate() {
    if (this._lastPrompt && !this.busy) this.retry();
  },

  openMessageSearch() {
    this.showChat();
    this.messageSearchOpen = true;
    this.$nextTick(() => {
      const input = this.$refs.messageSearchInput as HTMLInputElement | null;
      if (input) input.focus();
    });
  },

  closeMessageSearch() {
    this.messageSearchOpen = false;
    this.messageQuery = '';
    this.messageMatches = [];
    this.messageMatchIndex = 0;
    for (const message of this.messages) {
      message._hit = false;
      message._renderClass = message._cls || '';
    }
  },

  searchMessages(query) {
    this.messageQuery = query;
    if (!String(query || '').trim()) {
      this.messageMatches = [];
      this.messageMatchIndex = 0;
      return;
    }
    this.messageMatches = this.messages
      .filter((message) => includesQuery(
        `${message.text || ''} ${message.summary || ''} ${message.name || ''}`,
        query,
      ))
      .map((message) => message.id);
    const matched = new Set(this.messageMatches);
    for (const message of this.messages) {
      message._hit = matched.has(message.id);
      message._renderClass = `${message._cls || ''}${message._hit ? ' msg-hit' : ''}`;
    }
    this.messageMatchIndex = 0;
    this.scrollToMessageMatch();
  },

  scrollToMessageMatch() {
    const id = this.messageMatches[this.messageMatchIndex];
    if (id === undefined) return;
    const index = this.messages.findIndex((message) => message.id === id);
    if (index >= 0) this.scrollToMessageIndex(index);
  },

  scrollToMessageIndex(index) {
    if (index < 0 || index >= this.messages.length) return;
    const root = this.$refs.transcript as HTMLElement | null;
    if (!root) return;
    if (this.virtualEnabled()) {
      const heights = this.virtualHeights();
      const offset = heights
        .slice(0, index)
        .reduce((sum, height) => sum + height, 0);
      root.scrollTop = Math.max(0, offset - 80);
      this.recomputeVirtualRange();
    }
    const id = this.messages[index].id;
    this.$nextTick(() => {
      const find = () => root.querySelector(
        `[data-message-id="${id}"]`,
      ) as HTMLElement | null;
      const first = find();
      if (first) {
        first.scrollIntoView({ block: 'center', behavior: 'smooth' });
        return;
      }
      this.$nextTick(() => {
        const second = find();
        if (second) {
          second.scrollIntoView({ block: 'center', behavior: 'smooth' });
        }
      });
    });
  },

  nextMessageMatch(delta) {
    if (!this.messageMatches.length) return;
    this.messageMatchIndex = (
      this.messageMatchIndex + delta + this.messageMatches.length
    ) % this.messageMatches.length;
    this.scrollToMessageMatch();
  },

  get messageMatchLabel() {
    if (!this.messageMatches.length) return this.messageQuery ? '0/0' : '';
    return `${this.messageMatchIndex + 1}/${this.messageMatches.length}`;
  },

};
