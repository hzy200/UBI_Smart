// Phase B5 首切片：与 events.py 对齐的最小事件/审批类型。
// 后续由 scripts/gen_contracts.py 生成完整 union；当前先供新代码引用。

export interface AgentEvent {
  type: string;
  [key: string]: unknown;
}

export interface RunStartedEvent extends AgentEvent {
  type: "run_started";
  thread_id: string;
}

export interface AssistantTokenEvent extends AgentEvent {
  type: "assistant_token";
  node: string;
  text: string;
}

export interface ReasoningTokenEvent extends AgentEvent {
  type: "reasoning_token";
  node: string;
  text: string;
}

export interface ApprovalRequestedEvent extends AgentEvent {
  type: "approval_requested";
  request_id: string;
  tool: string;
  command: string;
  level: string;
  reason: string;
}

export interface RunFinishedEvent extends AgentEvent {
  type: "run_finished";
  thread_id: string;
  answer: string;
  input_tokens?: number | null;
  output_tokens?: number | null;
}

export interface RunFailedEvent extends AgentEvent {
  type: "run_failed";
  message: string;
}

export interface SessionSummary {
  thread_id: string;
  title: string;
  workspace: string;
  last_active?: string;
  access_mode?: string;
  interrupted?: boolean;
}
