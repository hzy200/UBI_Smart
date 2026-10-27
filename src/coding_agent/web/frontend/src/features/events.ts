/* Alpine mixin: events。由 app.ts 拆分，方法名保持稳定以兼容模板与构建守卫。 */

import {
  formatDuration,
  formatTokens,
} from '../ui/format';
import {
  toolMap,
} from '../state/runtime';
import type {
  AgentEvent,
} from '../contracts';
import { includesQuery } from '../ui/search';

export const eventsMixin = {
  handleEvent(event: AgentEvent) {
    switch (event.type) {
      case 'run_started':
        this.stateText = '运行中…';
        this.connectionState = 'streaming';
        this._terminal = false;
        this._suspended = false;
        this.timeline = [];
        this.pushTimeline('start', '开始运行');
        break;
      case 'plan_created':
        this.plan = event.steps || [];
        this.kinds = event.kinds || [];
        this.planRows = this.buildPlanRows(this.plan, this.kinds, 0);
        this.planDegraded = !!event.degraded;
        this.stepIdx = 0;
        this.setTab('plan', false);
        this.pushTimeline('plan', '生成计划', `${this.plan.length} 步`);
        break;
      case 'plan_revised':
        this.plan = event.steps || [];
        this.kinds = event.kinds || [];
        this.stepIdx = event.step_idx || 0;
        this.planRows = this.buildPlanRows(this.plan, this.kinds, this.stepIdx);
        this.pushMessage('system', '计划已修订');
        break;
      case 'step_started':
        this.stepIdx = event.index || 0;
        this.planRows = this.buildPlanRows(this.plan, this.kinds, this.stepIdx);
        if ((event.total || 1) > 1) {
          this.pushMessage('system', `第 ${event.index + 1}/${event.total} 步：${event.text}`);
        }
        this.pushTimeline(
          'step',
          `第 ${event.index + 1}/${event.total || 1} 步`,
          event.text,
        );
        break;
      case 'step_finished':
        if (event.cancelled) this.pushMessage('system', '该步骤被取消');
        else if (event.budget_exhausted) this.pushMessage('system', '该步骤工具预算耗尽');
        break;
      case 'assistant_token':
        this.appendAssistantToken(event.node, event.text);
        break;
      case 'reasoning_token': {
        const node = event.node || '';
        let block = this.reasoningBlocks[this.reasoningBlocks.length - 1];
        if (!block || block.node !== node) {
          block = { node, text: '' };
          this.reasoningBlocks.push(block);
        }
        block.text += event.text || '';
        break;
      }
      case 'tool_call_started':
        this.upsertTool(event);
        this.upsertToolMessage(event);
        this.setTab('tools', false);
        this.pushTimeline('tool', event.name, event.summary);
        break;
      case 'tool_call_finished':
        this.finishTool(event);
        this.finishToolMessage(event);
        this.pushTimeline(
          event.ok ? 'ok' : 'error',
          event.name,
          event.exit_code != null ? `exit ${event.exit_code}` : '',
        );
        break;
      case 'file_changed':
        this.files.unshift({ ...event, _key: `${event.snapshot_id}:${event.path}` });
        this.filesDirty = true;
        break;
      case 'verification':
        this.pushMessage('system', this.verificationText(event));
        this.pushTimeline(
          event.ok === false ? 'error' : 'verify',
          '验证',
          event.summary,
        );
        break;
      case 'review_finished':
        this.pushMessage('system', this.reviewText(event));
        this.pushTimeline(
          event.blocked ? 'error' : 'review',
          '审查',
          event.summary,
        );
        break;
      case 'repair_started':
        this.pushMessage('system', `第 ${event.attempt}/${event.limit} 次修复：${event.summary}`);
        break;
      case 'task_graph_created':
        this.tasks = (event.tasks || []).map((task) => this.decorateTask(task));
        this.tasksDirty = true;
        break;
      case 'task_graph_extended':
        this.tasks = this.tasks.concat((event.added || []).map((task) => this.decorateTask(task)));
        break;
      case 'worker_started':
        this.upsertWorker(event);
        break;
      case 'worker_finished':
        this.finishWorker(event);
        break;
      case 'subagent_started':
        this.upsertTool({
          call_id: event.agent_id,
          name: `subagent:${event.role}`,
          summary: event.goal,
          level: '只读',
        });
        break;
      case 'subagent_finished':
        this.finishTool({
          call_id: event.agent_id,
          name: 'subagent',
          ok: event.status === 'success',
          preview: event.summary,
        });
        break;
      case 'orchestration_finished':
        this.pushMessage('system', `编排完成：${event.completed} 成功 / ${event.failed} 失败`);
        break;
      case 'workflow_selected':
        this.pushMessage('system', `工作流：${event.workflow}${event.reason ? '（' + event.reason + '）' : ''}`);
        break;
      case 'context_trimmed':
        if (event.trimmed) {
          this.pushMessage('system', `上下文裁剪：${event.trimmed} 条，${event.before_tokens} → ${event.after_tokens} tokens`);
          this.pushTimeline(
            'info',
            '上下文裁剪',
            `${event.before_tokens} → ${event.after_tokens} tokens`,
          );
        }
        break;
      case 'cost_report':
        this.costText = this.formatCost(event);
        this.usage = {
          input: Number(event.input_tokens || 0),
          output: Number(event.output_tokens || 0),
          cost: Number(event.cost_usd || 0),
          budget: Number(event.budget_usd || 0),
          ratio: Number(event.ratio || 0),
          cache: Number(event.cache_hit_tokens || 0),
        };
        for (const alert of event.alerts || []) this.toastMessage(alert);
        this.pushTimeline('cost', '成本', this.costText);
        break;
      case 'approval_requested':
        this.approvals.push(event);
        this.showNextApproval();
        this.stateText = '等待审批';
        this._suspended = true;
        this._terminal = false;
        this.pushTimeline(
          'warning',
          '等待审批',
          event.command || event.tool || '',
        );
        this.notify('CodingAgent 需要审批', event.command || event.tool || '');
        break;
      case 'steering_applied':
        if (event.action === 'pause') {
          this.paused = true;
          this.stateText = '已暂停';
          this._suspended = true;
          this.pushMessage('system', `已暂停：${event.detail || ''}`);
        } else if (event.action === 'resume') {
          this.paused = false;
          this.stateText = '运行中…';
          this._suspended = false;
          this._terminal = false;
        } else {
          this.pushMessage('system', `steering：${event.action} ${event.detail || ''}`);
        }
        break;
      case 'run_finished':
        this.stateText = '就绪';
        this.busy = false;
        this._terminal = true;
        this._suspended = false;
        this.connectionState = 'idle';
        if (this._currentMessage) {
          this.flushAssistantToken(this._currentMessage.id);
        }
        this.dedupeAssistantMessages();
        if (event.answer && !this._currentMessage) {
          const last = this.messages[this.messages.length - 1];
          const same =
            last &&
            last.role === 'assistant' &&
            String(last.text || '').trim() ===
              String(event.answer || '').trim();
          if (!same) {
            this.pushMessage('assistant', event.answer, true, 'final');
          }
        }
        // 最后一轮 assistant 输出才是最终答案；之前的节点/步骤
        // 保持“工作中”样式，避免和最终回答混淆。
        this.markFinalAnswer();
        this.moveFinalAnswerLast();
        this.scrollBottom();
        this.pushTimeline('done', '运行完成', event.answer || '');
        void this.saveTranscript();
        this.notify('CodingAgent 运行完成', event.answer || '');
        break;
      case 'run_failed':
        this.stateText = '失败';
        this._terminal = true;
        this._suspended = false;
        this.connectionState = 'closed';
        this.pushMessage('system', `运行失败：${event.message}`);
        this.pushTimeline('error', '运行失败', event.message);
        void this.saveTranscript();
        this.notify('CodingAgent 运行失败', event.message || '');
        break;
      default:
        break;
    }
  },

  verificationText(event) {
    if (event.ok === true) return `验证通过：${event.summary}`;
    if (event.ok === false) return `验证失败：${event.summary}`;
    return `未验证：${event.status} ${event.summary}`;
  },

  reviewText(event) {
    if (event.blocked) return `审查阻断：${event.summary}`;
    if (event.status === 'warned') return `审查告警：${event.summary}`;
    if (event.status === 'clean') return '审查通过';
    return `审查：${event.status}`;
  },

  formatCost(event) {
    let text = `$${Number(event.cost_usd || 0).toFixed(4)}`;
    if (event.budget_usd > 0) {
      text += ` / $${Number(event.budget_usd).toFixed(2)}（${Math.round((event.ratio || 0) * 100)}%）`;
    }
    return text;
  },

  upsertTool(event) {
    const key = event.call_id || event.name || `${this.tools.length}`;
    const existing = toolMap.get(key);
    if (existing) {
      existing.name = event.name || existing.name;
      existing.summary = event.summary || existing.summary;
      existing.level = event.level || existing.level;
      existing._start = {
        ...(existing._start || {}),
        name: existing.name,
        summary: existing.summary,
        level: existing.level,
        args: event.args || (existing._start && existing._start.args) || {},
      };
      this.refreshToolDetail(existing);
      return;
    }
    const tool = {
      key,
      call_id: event.call_id || key,
      name: event.name || 'tool',
      summary: event.summary || '',
      level: event.level || '',
      state: 'running',
      preview: '',
      _cls: 'tool-running',
      _start: {
        call_id: event.call_id || key,
        name: event.name || 'tool',
        summary: event.summary || '',
        level: event.level || '',
        args: event.args || {},
      },
      _finish: null,
      _detail: '',
    };
    this.refreshToolDetail(tool);
    toolMap.set(key, tool);
    this.tools.unshift(tool);
  },

  finishTool(event) {
    const key = event.call_id || event.name || '';
    let tool = key ? toolMap.get(key) : null;
    if (!tool) {
      tool = {
        key: key || `${event.name}-${this.tools.length}`,
        call_id: key,
        name: event.name || 'tool',
        summary: '',
        level: event.level || '',
        state: 'running',
        preview: '',
      };
      toolMap.set(tool.key, tool);
      this.tools.unshift(tool);
    }
    if (event.rejected) tool.state = 'rejected';
    else if (event.skipped) tool.state = 'skipped';
    else if (event.interrupted) tool.state = 'interrupted';
    else if (event.ok) tool.state = 'ok';
    else tool.state = 'error';
    tool._cls = `tool-${tool.state}`;
    if (event.preview) tool.preview = event.preview;
    else if (event.exit_code !== null && event.exit_code !== undefined) {
      tool.preview = `exit=${event.exit_code}`;
    }
    tool._finish = {
      ok: Boolean(event.ok),
      rejected: Boolean(event.rejected),
      skipped: Boolean(event.skipped),
      interrupted: Boolean(event.interrupted),
      decision: event.decision || '',
      exit_code: event.exit_code,
      duration_ms: event.duration_ms,
      preview: String(event.preview || '').slice(0, 4000),
    };
    this.refreshToolDetail(tool);
  },

  refreshToolDetail(tool) {
    tool._detail = JSON.stringify({
      call_id: tool.call_id,
      name: tool.name,
      level: tool.level,
      state: tool.state,
      summary: tool.summary,
      start: tool._start || null,
      finish: tool._finish || null,
    }, null, 2);
  },

  setToolFilter(name) {
    this.toolFilter = name;
  },

  toolFilterClass(name) {
    return this.toolFilter === name ? 'active' : '';
  },

  filteredTools() {
    return this.tools.filter((tool) => {
      const state = tool.state || 'running';
      if (this.toolFilter === 'running' && state !== 'running') return false;
      if (this.toolFilter === 'ok' && state !== 'ok') return false;
      if (
        this.toolFilter === 'error'
        && !['error', 'rejected', 'interrupted'].includes(state)
      ) {
        return false;
      }
      return includesQuery(
        `${tool.name} ${tool.summary} ${tool.preview}`,
        this.toolQuery,
      );
    });
  },

  toolMessageKey(event) {
    return event.call_id || event.name || '';
  },

  upsertToolMessage(event) {
    const key = this.toolMessageKey(event);
    const existing = this.messages.find(
      (message) => message.kind === 'tool' && message.call_id === key,
    );
    if (existing) {
      existing.name = event.name || existing.name;
      existing.summary = event.summary || existing.summary;
      existing.level = event.level || existing.level;
      return;
    }
    const summary = event.summary || '';
    this.messages.push({
      id: ++this._msgSeq,
      role: '工具',
      phase: 'tool',
      kind: 'tool',
      call_id: key,
      name: event.name || 'tool',
      summary,
      level: event.level || '',
      state: '运行中',
      preview: '',
      text: summary,
      done: false,
      _expanded: false,
      _cls: 'msg-tool tool-running',
    });
    this.scrollBottom(false);
  },

  finishToolMessage(event) {
    const key = this.toolMessageKey(event);
    const message = [...this.messages].reverse().find(
      (item) => item.kind === 'tool' && item.call_id === key,
    );
    if (!message) return;
    let state = '失败';
    if (event.rejected) state = '已拒绝';
    else if (event.skipped) state = '跳过';
    else if (event.interrupted) state = '中断';
    else if (event.ok) state = '完成';
    message.state = state;
    message.ok = Boolean(event.ok);
    message.exit_code = event.exit_code ?? null;
    message.duration_ms = event.duration_ms ?? null;
    message.preview = event.preview || (
      event.exit_code !== null && event.exit_code !== undefined
        ? `exit=${event.exit_code}`
        : ''
    );
    message.text = [message.summary, message.preview]
      .filter(Boolean)
      .join('\n');
    message.done = true;
    message._cls =
      `msg-tool ${event.ok ? 'tool-ok' : 'tool-error'}`;
    if (event.rejected) message._cls = 'msg-tool tool-rejected';
    this.scrollBottom(false);
  },

  upsertWorker(event) {
    const id = event.task_id || event.worker_id;
    const existing = this.tasks.find((task) => (task.id || task.task_id) === id);
    if (existing) {
      existing.status = 'running';
      existing._status = 'running';
      existing._cls = 'task-running';
      existing.worker_id = event.worker_id || existing.worker_id;
      existing._worker = existing.worker_id || '';
      existing.attempt = Number(event.attempt || existing.attempt || 1);
      existing.queue_wait_ms = Number(event.queue_wait_ms || 0);
      return;
    }
    this.tasks.push(this.decorateTask({
      id,
      goal: event.goal,
      role: event.role,
      status: 'running',
      worker_id: event.worker_id,
      attempt: event.attempt,
      queue_wait_ms: event.queue_wait_ms,
    }));
  },

  finishWorker(event) {
    const id = event.task_id || event.worker_id;
    const existing = this.tasks.find((task) => (task.id || task.task_id) === id);
    if (!existing) return;
    existing.status = event.status || 'done';
    existing._status = existing.status;
    existing._cls = `task-${existing.status}`;
    existing.worker_id = event.worker_id || existing.worker_id || '';
    existing.merged = event.merged;
    existing.merge_error = event.merge_error || '';
    existing.summary = event.summary;
    existing.duration_ms = Number(event.duration_ms || existing.duration_ms || 0);
    existing.input_tokens = Number(event.input_tokens || 0);
    existing.output_tokens = Number(event.output_tokens || 0);
    existing.tool_calls = Number(event.tool_calls || 0);
    existing.attempt = Number(event.attempt || existing.attempt || 1);
    existing._worker = existing.worker_id;
    existing._timing = formatDuration(existing.duration_ms);
    existing._tokens = formatTokens(existing.input_tokens, existing.output_tokens);
  },

  pushTimeline(kind, title, detail = '') {
    this.timeline.push({
      id: ++this._timelineSeq,
      kind,
      title: String(title || ''),
      detail: String(detail || '').slice(0, 300),
      time: new Date().toLocaleTimeString(),
      _cls: `timeline-${kind}`,
    });
    if (this.timeline.length > 500) {
      this.timeline.splice(0, this.timeline.length - 500);
    }
  },

};
