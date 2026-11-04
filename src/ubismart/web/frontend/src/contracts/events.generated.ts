/* 由 scripts/gen_contracts.py 从 src/ubismart/events.py 生成，禁止手改。 */

export interface ApprovalRequestedEvent {
  type: "approval_requested";
  request_id: string;
  tool: string;
  command: string;
  level: string;
  level_int: number;
  reason: string;
  plan_version: number;
  ack_timeout_ms: number;
  default_action: string;
}

export interface AssistantTokenEvent {
  type: "assistant_token";
  node: string;
  text: string;
}

export interface BudgetWarningEvent {
  type: "budget_warning";
  level: any;
  dimension: string;
  used: number;
  limit: number;
  ratio: number;
  message: string;
}

export interface CardEventEvent {
  type: "card";
  event_id: string;
  card_id: string;
  kind: any;
  severity: any;
  title: string;
  body: string;
  payload: Record<string, any>;
  dedupe_key: string;
  plan_version: number;
  requires_ack: boolean;
  ack_timeout_ms: number;
  default_action: string;
}

export interface ContextTrimmedEvent {
  type: "context_trimmed";
  node: string;
  trimmed: number;
  before_chars: number;
  after_chars: number;
  before_tokens: number;
  after_tokens: number;
  escalated: boolean;
}

export interface CostReportEvent {
  type: "cost_report";
  run_id: string;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  budget_usd: number;
  ratio: number;
  billed_usd: number;
  billing_delta_usd: number;
  billing_error: string;
  alerts: Array<string>;
  cache_hit_tokens: number;
  cache_miss_tokens: number;
  providers: Record<string, Record<string, number>>;
  billing_providers: Record<string, Record<string, number>>;
}

export interface EstimationReadyEvent {
  type: "estimation_ready";
  card: Record<string, any>;
  plan_version: number;
}

export interface FileChangedEvent {
  type: "file_changed";
  path: string;
  action: string;
  added: number;
  removed: number;
  diff: string;
  snapshot_id: string | null;
}

export interface LoopDetectedEvent {
  type: "loop_detected";
  task_id: string;
  tool: string;
  count: number;
  reason: string;
}

export interface OrchestrationFinishedEvent {
  type: "orchestration_finished";
  completed: number;
  failed: number;
  summary: string;
}

export interface PlanCreatedEvent {
  type: "plan_created";
  steps: Array<string>;
  kinds: Array<string>;
  degraded: boolean;
  plan_version: number;
}

export interface PlanRevisedEvent {
  type: "plan_revised";
  steps: Array<string>;
  kinds: Array<string>;
  step_idx: number;
  plan_version: number;
}

export interface ReasoningTokenEvent {
  type: "reasoning_token";
  node: string;
  text: string;
}

export interface RepairStartedEvent {
  type: "repair_started";
  attempt: number;
  limit: number;
  summary: string;
  issues: Array<string>;
}

export interface ReviewFinishedEvent {
  type: "review_finished";
  status: string;
  blocked: boolean;
  summary: string;
  findings: Array<string>;
}

export interface RunFailedEvent {
  type: "run_failed";
  message: string;
  input_tokens: number | null;
  output_tokens: number | null;
  worker_input_tokens: number | null;
  worker_output_tokens: number | null;
}

export interface RunFinishedEvent {
  type: "run_finished";
  thread_id: string;
  answer: string;
  input_tokens: number | null;
  output_tokens: number | null;
  worker_input_tokens: number | null;
  worker_output_tokens: number | null;
  concurrency: Array<Record<string, any>> | null;
}

export interface RunStartedEvent {
  type: "run_started";
  thread_id: string;
}

export interface StageDoneEvent {
  type: "stage_done";
  stage: number;
  status: string;
  reason: string;
  completed: number;
  failed: number;
  checkpoint_id: string;
  plan_version: number;
}

export interface SteeringAppliedEvent {
  type: "steering_applied";
  action: string;
  task_id: string;
  detail: string;
}

export interface StepFinishedEvent {
  type: "step_finished";
  index: number;
  budget_exhausted: boolean;
  cancelled: boolean;
  text: string;
}

export interface StepStartedEvent {
  type: "step_started";
  index: number;
  total: number;
  text: string;
}

export interface SubAgentFinishedEvent {
  type: "subagent_finished";
  agent_id: string;
  status: string;
  summary: string;
  relevant_files: Array<string>;
  findings: Array<string>;
  input_tokens: number;
  output_tokens: number;
  duration_ms: number;
}

export interface SubAgentStartedEvent {
  type: "subagent_started";
  agent_id: string;
  parent_call_id: string;
  role: string;
  goal: string;
}

export interface TaskBlockedEvent {
  type: "task_blocked";
  task_id: string;
  role: string;
  reason: string;
}

export interface TaskGraphCreatedEvent {
  type: "task_graph_created";
  tasks: Array<Record<string, any>>;
  degraded: boolean;
  plan_version: number;
}

export interface TaskGraphExtendedEvent {
  type: "task_graph_extended";
  added: Array<Record<string, any>>;
  rejected: Array<string>;
}

export interface ToolCallFinishedEvent {
  type: "tool_call_finished";
  call_id: string;
  name: string;
  ok: boolean;
  rejected: boolean;
  skipped: boolean;
  interrupted: boolean;
  decision: string;
  exit_code: number | null;
  duration_ms: number | null;
  level: string | null;
  preview: string;
}

export interface ToolCallStartedEvent {
  type: "tool_call_started";
  call_id: string;
  name: string;
  args: Record<string, any>;
  summary: string;
  level: string | null;
}

export interface VerificationEvent {
  type: "verification";
  status: string;
  command: string;
  ok: boolean | null;
  summary: string;
  issues: Array<string>;
  failed_tests: Array<string>;
}

export interface WorkerFinishedEvent {
  type: "worker_finished";
  worker_id: string;
  task_id: string;
  role: string;
  status: string;
  summary: string;
  input_tokens: number;
  output_tokens: number;
  duration_ms: number;
  attempt: number;
  merged: boolean;
  merge_error: string;
  tool_calls: number;
  queue_wait_ms: number;
  llm_ms: number;
  tool_ms: number;
  timed_out: boolean;
  artifact_id: string;
  depth: number;
  priority: number;
  scope_violations: Array<string>;
}

export interface WorkerStartedEvent {
  type: "worker_started";
  worker_id: string;
  task_id: string;
  role: string;
  goal: string;
  attempt: number;
  queue_wait_ms: number;
}

export interface WorkflowRevisedEvent {
  type: "workflow_revised";
  reason: string;
  tasks: Array<Record<string, any>>;
  plan_version: number;
}

export interface WorkflowSelectedEvent {
  type: "workflow_selected";
  workflow: string;
  reason: string;
}

export type AgentEvent =
  | ApprovalRequestedEvent
  | AssistantTokenEvent
  | BudgetWarningEvent
  | CardEventEvent
  | ContextTrimmedEvent
  | CostReportEvent
  | EstimationReadyEvent
  | FileChangedEvent
  | LoopDetectedEvent
  | OrchestrationFinishedEvent
  | PlanCreatedEvent
  | PlanRevisedEvent
  | ReasoningTokenEvent
  | RepairStartedEvent
  | ReviewFinishedEvent
  | RunFailedEvent
  | RunFinishedEvent
  | RunStartedEvent
  | StageDoneEvent
  | SteeringAppliedEvent
  | StepFinishedEvent
  | StepStartedEvent
  | SubAgentFinishedEvent
  | SubAgentStartedEvent
  | TaskBlockedEvent
  | TaskGraphCreatedEvent
  | TaskGraphExtendedEvent
  | ToolCallFinishedEvent
  | ToolCallStartedEvent
  | VerificationEvent
  | WorkerFinishedEvent
  | WorkerStartedEvent
  | WorkflowRevisedEvent
  | WorkflowSelectedEvent;

export const AGENT_EVENT_TYPES = [
  "approval_requested",
  "assistant_token",
  "budget_warning",
  "card",
  "context_trimmed",
  "cost_report",
  "estimation_ready",
  "file_changed",
  "loop_detected",
  "orchestration_finished",
  "plan_created",
  "plan_revised",
  "reasoning_token",
  "repair_started",
  "review_finished",
  "run_failed",
  "run_finished",
  "run_started",
  "stage_done",
  "steering_applied",
  "step_finished",
  "step_started",
  "subagent_finished",
  "subagent_started",
  "task_blocked",
  "task_graph_created",
  "task_graph_extended",
  "tool_call_finished",
  "tool_call_started",
  "verification",
  "worker_finished",
  "worker_started",
  "workflow_revised",
  "workflow_selected",
] as const;

export type AgentEventType = (typeof AGENT_EVENT_TYPES)[number];
