// 事件类型由 scripts/gen_contracts.py 从 src/ubismart/events.py 生成。
// 这里只保留前端自己的非事件 DTO。

export * from './contracts/events.generated';

export interface SessionSummary {
  thread_id: string;
  title: string;
  workspace: string;
  last_active?: string;
  access_mode?: string;
  interrupted?: boolean;
}
