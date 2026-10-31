/* 单会话视图模型。
 *
 * 会话级字段集中在这里；app 通过 facade getter/setter 代理到 activeView，
 * SessionStore 按 threadId 复用视图，SSE 与审批绑定发起视图。主区与分屏
 * 可同时运行两个视图。
 */

export function createSessionView(threadId = '') {
  return {
    threadId,
    loaded: false,
    _cacheOnly: false,
    historyHasMore: false,
    historyLoading: false,
    _historyItems: [],
    _historyOffset: 0,
    lastActiveAt: 0,
    _abort: null,
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

    // 编辑器/文件视图按会话隔离：切走再切回时标签仍在。
    mainTab: 'chat',
    mainTabFlags: { chat: true, file: false, diff: false },
    mainTabClasses: { chat: 'active', file: '', diff: '' },
    editorTabs: [],
    activeEditorId: '',
    diffPath: '',
    diffText: '',
    diffDisplay: '',
    diffTruncated: false,
    diffExpanded: false,
    diffRowsData: [],
    splitDiffRowsData: [],
    diffView: 'unified',
    diffViewFlags: { unified: true, split: false },
    diffViewClasses: { unified: 'active', split: '' },
    fileView: {
      path: '', size: 0, _text: '', _html: '',
      _lang: '', _size: '', _lineNumbers: '', _crumb: '',
      _isImage: false, _isText: true, _imageNote: '',
      data_url: '', mime: '',
    },
    fileLineInput: '',
    fileLoading: false,

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
    timeline: [],
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
