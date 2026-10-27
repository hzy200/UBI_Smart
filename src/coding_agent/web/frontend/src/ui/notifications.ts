/* 系统通知的触发条件。
 *
 * 站内通知历史始终记录；只有用户明确开启、页面不可见且权限已授予时，
 * 才额外弹系统通知，避免正在看页面时重复打扰。
 */

export interface SystemNotifyState {
  enabled: boolean;
  hidden: boolean;
  supported: boolean;
  permission: string;
}

export function shouldSystemNotify(state: SystemNotifyState): boolean {
  return Boolean(
    state.enabled
    && state.hidden
    && state.supported
    && state.permission === 'granted',
  );
}
