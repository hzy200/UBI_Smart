/* Alpine mixin: transcript。由 app.ts 拆分，方法名保持稳定以兼容模板与构建守卫。 */

import {
  renderMarkdown,
} from '../ui/format';
import {
  renderTimers,
  tokenBuffers,
} from '../state/runtime';

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
    this.messages.push(message);
    this.scrollBottom();
    this.$nextTick(() => this.decorateTranscript());
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
      if (pre.dataset.copyReady) return;
      pre.dataset.copyReady = '1';
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'copy-code';
      button.textContent = '复制代码';
      button.setAttribute('aria-label', '复制代码块');
      button.addEventListener('click', () => this.copyCode(pre));
      pre.insertAdjacentElement('beforebegin', button);
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
      this.messages.push(this._currentMessage);
      tokenBuffers.set(this._currentMessage.id, '');
    }
    const id = this._currentMessage.id;
    // 每个 token 只追加到模块级缓冲，避免逐 token 触发 Alpine 响应式
    // 更新和整段 Markdown 重解析。用节流而不是防抖：token 连续到达时
    // 仍然每 60ms 刷新一次，而不是等流结束才一次性出现。
    tokenBuffers.set(id, (tokenBuffers.get(id) || '') + text);
    if (renderTimers.has(id)) return;
    renderTimers.set(id, setTimeout(() => {
      renderTimers.delete(id);
      const target = this.messages.find((item) => item.id === id);
      const raw = tokenBuffers.get(id) || '';
      if (target) {
        target.text = raw;
        target.html = renderMarkdown(raw);
      }
      this.scrollBottom(false);
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
      target.text = raw;
      target.html = renderMarkdown(raw);
    }
    tokenBuffers.delete(id);
  },

  finishAssistant() {
    if (this._currentMessage) {
      this.flushAssistantToken(this._currentMessage.id);
      this._currentMessage.done = true;
    }
    this._currentMessage = null;
    this._currentNode = '';
    this.$nextTick(() => this.decorateTranscript());
  },

  scrollBottom(force = true) {
    this.$nextTick(() => {
      const element = this.$refs.transcript;
      if (!element) return;
      if (!force && this.showJumpLatest) return;
      element.scrollTop = element.scrollHeight;
      this.showJumpLatest = false;
    });
  },

  onTranscriptScroll(event) {
    const element = event && event.target;
    if (!element) return;
    const remaining = element.scrollHeight - element.scrollTop - element.clientHeight;
    this.showJumpLatest = remaining > 120;
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
  },

  searchMessages(query) {
    this.messageQuery = query;
    const needle = String(query || '').trim().toLowerCase();
    if (!needle) {
      this.messageMatches = [];
      this.messageMatchIndex = 0;
      return;
    }
    this.messageMatches = this.messages
      .filter((message) => {
        const haystack = (
          `${message.text || ''} ${message.summary || ''} ${message.name || ''}`
        ).toLowerCase();
        return haystack.includes(needle);
      })
      .map((message) => message.id);
    this.messageMatchIndex = 0;
    this.scrollToMessageMatch();
  },

  scrollToMessageMatch() {
    const id = this.messageMatches[this.messageMatchIndex];
    if (id === undefined) return;
    this.$nextTick(() => {
      const element = document.querySelector(
        `[data-message-id="${id}"]`,
      ) as HTMLElement | null;
      if (element) element.scrollIntoView({ block: 'center', behavior: 'smooth' });
    });
  },

  nextMessageMatch(delta) {
    if (!this.messageMatches.length) return;
    this.messageMatchIndex = (
      this.messageMatchIndex + delta + this.messageMatches.length
    ) % this.messageMatches.length;
    this.scrollToMessageMatch();
  },

  messageMatchLabel() {
    if (!this.messageMatches.length) return this.messageQuery ? '0/0' : '';
    return `${this.messageMatchIndex + 1}/${this.messageMatches.length}`;
  },

};
