export interface AdminUser {
  id: string;
  username: string;
  email: string;
  role: 'owner' | 'admin' | 'member';
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

export interface UsageRow extends UsageToday {
  user: string;
}

export interface UsageReport {
  rows: UsageRow[];
  totals: UsageToday;
}
