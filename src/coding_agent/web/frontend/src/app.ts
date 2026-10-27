/* CodingAgent GUI entry.
 *
 * 只消费后端领域事件，不接触 LangGraph / 工具 / 沙箱。
 * Alpine 使用 CSP 构建：模板里尽量只调用组件方法，避免 eval。
 */

import { bootMixin } from './features/boot';
import { authMixin } from './features/auth';
import { settingsMixin } from './features/settings';
import { workspaceMixin } from './features/workspace';
import { view_modelMixin } from './features/view-model';
import { systemMixin } from './features/system';
import { sessionsMixin } from './features/sessions';
import { transcriptMixin } from './features/transcript';
import { runMixin } from './features/run';
import { eventsMixin } from './features/events';
import { approvalsMixin } from './features/approvals';
import { steeringMixin } from './features/steering';
import { uploadsMixin } from './features/uploads';
import { inspectorMixin } from './features/inspector';
import { recoveryMixin } from './features/recovery';
import { providersMixin } from './features/providers';
import { desktopMixin } from './features/desktop';
import { memoryMixin } from './features/memory';
import { keyboardMixin } from './features/keyboard';
import { focusMixin } from './features/focus';
import { errorsMixin } from './features/errors';
import { layoutMixin } from './features/layout';
import { notificationsMixin } from './features/notifications';
import { cspMixin } from './features/csp';

const agentApp = () => {
  const app: Record<string, any> = ({
  ready: false,
  configLoaded: false,
  configError: '',
  configErrorCode: '',
  versionChecked: false,
  versionError: '',
  versionErrorCode: '',
  apiVersion: '',
  minFrontendVersion: '',
  serverVersion: '',
  capabilitiesVersion: 0,
  authMode: 'bearer',
  authLoginUrl: '',
  authRefreshUrl: '',
  authLogoutUrl: '',
  authCsrfHeader: 'X-CSRF-Token',
  loginError: '',
  tokenInput: '',
  _authFailureAt: 0,
  healthText: '连接中…',
  workspace: '',
  workspaceInput: '',
  accessMode: 'read_only',
  model: '',
  provider: '',
  providers: [],
  capabilities: {
    read_only: true,
    server_write_enabled: false,
    multi_user: false,
    desktop_mode: false,
    workspace_registry: false,
    host_mode: false,
    workspace_upload_max_bytes: 0,
  },
  project: null,
  projectReport: '正在探测工作区…',
  projectWarnings: '',
  projectHasWarnings: false,
  projectLoading: false,
  projects: [],
  projectId: '',
  projectFile: null,
  projectFileName: '',
  projectBusy: false,
  projectMessage: '',
  projectJobKind: '',
  projectJobId: '',
  projectJobWorkspaceId: '',
  projectDropActive: false,
  projectPercent: 0,
  projectTransferPercent: 0,
  projectProgressText: '',
  revisions: [],
  revisionBusy: false,
  desktopAvailable: false,
  canCopyWorkspace: false,
  copyBusy: false,
  copyMessage: '',
  threadId: '',
  stateText: '就绪',
  costText: '',
  usage: { input: 0, output: 0, cost: 0, budget: 0, ratio: 0, cache: 0 },
  busy: false,
  paused: false,
  reasoningBlocks: [],
  showReasoning: false,
  lastSeq: 0,
  canReconnect: false,
  connectionState: 'idle',
  shortcutsOpen: false,
  _terminal: false,
  _suspended: false,
  _retryCount: 0,
  _retryTimer: null,
  _lastPrompt: '',
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
  diffText: '',
  tasks: [],
  memory: [],
  memoryText: '',
  audit: [],
  uploads: [],
  sessions: [],
  sessionRows: [],
  sessionQuery: '',
  sessionFilter: 'all',
  sessionFilterClasses: { all: 'active', pinned: '', archived: '' },
  sessionSelected: {},
  sessionBatchBusy: false,
  treeChildren: {},
  treeExpanded: {},
  treeLoadingMap: {},
  treeQuery: '',
  treeGit: {},
  treeGitState: '',
  treeActivePath: '',
  _treeGitLoaded: false,
  fileView: {
    path: '', size: 0, _text: '', _html: '',
    _lang: '', _size: '', _lineNumbers: '', _crumb: '',
  },
  gitInfo: {
    is_repo: false, _state: '', _branch: '',
    _changes: [], _commits: [],
  },
  gitDiffText: '',
  gitLoading: false,
  diffDisplay: '',
  diffTruncated: false,
  diffExpanded: false,
  updateText: '',
  updateInfo: null,
  diagnosticsPath: '',
  diagnosticsBusy: false,
  uploadQuota: '',
  showRecovery: false,
  recoveryItems: [],
  profiles: [],
  profilesEnabled: false,
  profileName: '',
  profileBaseUrl: '',
  profileModel: '',
  modelOptions: [],
  modelOptionsText: '',
  notifyEnabled: false,
  limitsText: '',
  approvals: [],
  approval: null,
  approvalPosition: '',
  decisions: {},
  tab: 'plan',
  mainTab: 'chat',
  editorTabs: [],
  activeEditorId: '',
  diffPath: '',
  view: 'sessions',
  viewClasses: { sessions: 'active', files: '', tree: '', git: '', tasks: '', memory: '', audit: '', problems: '' },
  viewFlags: { sessions: true, files: false, tree: false, git: false, tasks: false, memory: false, audit: false, problems: false },
  settingsTabFlags: { appearance: true, models: false, workspace: false, diagnostics: false, advanced: false },
  settingsTabClasses: { appearance: 'active', models: '', workspace: '', diagnostics: '', advanced: '' },
  tabFlags: { plan: true, tools: false, timeline: false },
  mainTabFlags: { chat: true, file: false, diff: false },
  mainTabClasses: { chat: 'active', file: '', diff: '' },
  diffRowsData: [],
  splitDiffRowsData: [],
  diffView: 'unified',
  diffViewFlags: { unified: true, split: false },
  diffViewClasses: { unified: 'active', split: '' },
  fileLineInput: '',
  problems: [],
  problemExpanded: {},
  _problemSeq: 0,
  filesDirty: false,
  tasksDirty: false,
  sidebarWidth: 280,
  inspectorWidth: 340,
  sidebarCollapsed: false,
  inspectorCollapsed: false,
  commandOpen: false,
  commandQuery: '',
  commandIndex: 0,
  liveAnnouncement: '',
  _modalReturnFocus: null,
  recentCommands: [],
  notifications: [],
  notificationsOpen: false,
  unreadNotifications: 0,
  _notificationSeq: 0,
  fileSearchResults: [],
  fileSearchLoading: false,
  _searchTimer: null,
  _searchSeq: 0,
  _resizing: '',
  showSettings: false,
  settingsTab: 'appearance',
  settingsQuery: '',
  sessionsLoading: false,
  fileLoading: false,
  uiError: '',
  _errorRetryKind: '',
  _errorRetryPayload: '',
  providerName: 'deepseek',
  providerKeyInput: '',
  theme: 'dark',
  density: 'normal',
  fontSize: 'normal',
  fontFamily: 'system',
  bodyClasses: 'theme-dark density-normal font-normal family-system',
  tabClasses: { plan: 'active', tools: '', timeline: '' },
  timeline: [],
  _timelineSeq: 0,
  badgeClasses: 'badge-read',
  toast: '',
  _msgSeq: 0,
  _currentNode: '',
  _currentMessage: null,
  _toastTimer: null,
  _boundProjectId: '',
  _composing: false,
  _compositionEndAt: 0,
  _consentTimer: null,
  consents: [],
  consentSupported: true,
  });
  const mixins = [
    bootMixin,
    authMixin,
    settingsMixin,
    workspaceMixin,
    view_modelMixin,
    systemMixin,
    sessionsMixin,
    transcriptMixin,
    runMixin,
    eventsMixin,
    approvalsMixin,
    steeringMixin,
    uploadsMixin,
    inspectorMixin,
    recoveryMixin,
    providersMixin,
    desktopMixin,
    memoryMixin,
    keyboardMixin,
    focusMixin,
    errorsMixin,
    layoutMixin,
    notificationsMixin,
    cspMixin,
  ];
  // 不能用对象展开：展开会在构造时求值 getter，失去响应性。
  // 用属性描述符保留 getter/method 的原始定义。
  for (const mixin of mixins) {
    Object.defineProperties(app, Object.getOwnPropertyDescriptors(mixin));
  }
  return app;
};

const registerAgentApp = () => {
  if (!window.Alpine) return;
  window.Alpine.data('agentApp', agentApp);
};

const startAlpine = (window as any).__alpineStart;
if (typeof startAlpine === 'function') {
  registerAgentApp();
  startAlpine();
} else if (window.Alpine) {
  registerAgentApp();
} else {
  document.addEventListener('alpine:init', registerAgentApp);
}
