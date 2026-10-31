export interface AdminUser {
  id: string;
  username: string;
  email: string;
  role: 'owner' | 'admin' | 'approver' | 'auditor' | 'member';
  status: 'active' | 'disabled';
  created_at: string;
  last_login_at: string;
}

export interface Quota {
  daily_tokens: number;
  daily_cost_cny: number;
  enabled: boolean;
}

export interface UsageToday {
  day: string;
  input_tokens: number;
  output_tokens: number;
  worker_input_tokens: number;
  worker_output_tokens: number;
  tokens: number;
  cost_usd: number;
  billed_usd: number;
  cost_cny: number;
  billed_cny: number;
  currency: string;
  runs: number;
  reserved_tokens: number;
  reserved_usd: number;
}

export interface AuthEvent {
  id: string;
  username: string;
  event: string;
  ip: string;
  request_id: string;
  ts: string;
}

export interface ProviderKeyRow {
  provider: string;
  model: string;
  base_url: string;
  has_key: boolean;
  last4: string;
}

export interface Me {
  user: AdminUser;
  quotas: Quota;
  usage_today: UsageToday;
}

export interface LoginResponse {
  ok?: boolean;
  mfa_required?: boolean;
  mfa_token?: string;
  user?: AdminUser;
  csrf_token?: string;
}

export interface ApprovalRow {
  id: string;
  tenant: string;
  thread_id: string;
  call_id: string;
  tool: string;
  command: string;
  level: string;
  level_int: number;
  requested_by: string;
  status: string;
  decided_by: string;
  decided_at: string;
  created_at: string;
  expires_at: string;
  created_at_iso?: string;
  decided_at_iso?: string;
  expires_at_iso?: string;
}

export interface UsageRow extends UsageToday {
  user: string;
}

export interface UsageReport {
  rows: UsageRow[];
  totals: UsageToday;
}

export interface AuditRecord {
  tenant: string;
  thread_id: string;
  ts: string;
  kind: string;
  tool: string;
  level: string;
  decision: string;
  ok: boolean | null;
  record_hash: string;
}

export interface AuditReport {
  records: AuditRecord[];
  total: number;
  has_more: boolean;
}

export interface HealthReport {
  ok: boolean;
  time: string;
  api_version: string;
  database: { ok: boolean; error: string };
  metrics: Record<string, number>;
}

export interface WorkspaceRow {
  workspace_id: string;
  tenant: string;
  owner: string;
  slug: string;
  display_name: string;
  source: string;
  kind: string;
  root_path?: string;
  status: string;
  access_mode: string;
  quota_bytes: number;
  used_bytes: number;
  file_count: number;
  acl_role: string;
  created_at: string;
  updated_at: string;
}

export interface WorkspaceAcl {
  id: string;
  tenant: string;
  workspace_id: string;
  grantee: string;
  permission: string;
  granted_by: string;
  created_at: string;
}
