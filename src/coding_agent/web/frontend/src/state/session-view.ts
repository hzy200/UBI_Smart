/* 单会话视图模型：阶段 1 只做状态归属，尚未并发多流。
 *
 * 会话级字段集中在这里；app.ts 通过 facade getter/setter 代理到 activeView，
 * 模板与现有 mixin 暂时无需改动。阶段 3 会把 activeView 换成按 threadId
 * 的 SessionStore，并去掉临时切换。
 */

export function createSessionView(threadId = '') {
  return {
    threadId,
    loaded: false,
    lastActiveAt: 0,
    stateText: '就绪',
    costText: '',
    usage: { input: 0, output: 0, cost: 0, budget: 0, ratio: 0, cache: 0 },
    busy: false,
    paused: false,
    reasoningBlocks: [],
    lastSeq: 0,
    canReconnect: false,
    connectionState: 'idle',
    _terminal: false,
    _suspended: false,
    _retryCount: 0,
    _retryTimer: null,
    _lastPrompt: '',
    _currentMessage: null,
    _currentNode: '',
    prompt: '',

    messages: [],
    virtualStart: 0,
    virtualEnd: 0,
    virtualTop: 0,
    virtualBottom: 0,
    _virtualRaf: 0,
    messageSearchOpen: false,
    messageQuery: '',
    messageMatches: [],
    messageMatchIndex: 0,
    showJumpLatest: false,

    plan: [],
    kinds: [],
    planRows: [],
    stepIdx: -1,
    planDegraded: false,

    tools: [],
    toolFilter: 'all',
    toolFilterClasses: { all: 'active', running: '', ok: '', error: '' },
    toolQuery: '',
    _toolsVersion: 0,

    files: [],
    checkpoints: [],
    filesDirty: false,
    tasks: [],
    tasksDirty: false,

    uploads: [],
    uploadQuota: '',
    approvals: [],
    approval: null,
    approvalPosition: '',
    decisions: {},

    problems: [],
    problemExpanded: {},
  };
}
