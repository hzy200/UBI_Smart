/* 多标签页同步：通知、设置与会话列表。
 *
 * 只同步“共享事实”，不同步活跃会话——避免一个标签页把另一个标签页
 * 正在看的会话强行切走。BroadcastChannel 不可用时静默退化为单标签行为。
 */

import { parseSyncMessage } from '../ui/multi-tab';

const CHANNEL = 'codingagent.sync';

export const multiTabMixin = {
  _syncChannel: null,

  initMultiTab() {
    if (typeof BroadcastChannel === 'undefined') return;
    try {
      this._syncChannel = new BroadcastChannel(CHANNEL);
      this._syncChannel.onmessage = (event) => {
        this.onSyncMessage(parseSyncMessage(event.data));
      };
    } catch (err) {
      this._syncChannel = null;
    }
  },

  broadcastSync(type, payload: Record<string, unknown> = {}) {
    if (!this._syncChannel) return;
    try {
      this._syncChannel.postMessage({ type, ...payload });
    } catch (err) {
      // 标签页正在关闭时可能抛错；同步是增强能力。
    }
  },

  onSyncMessage(message) {
    if (!message) return;
    if (message.type === 'sessions') {
      void this.loadSessions(true);
      return;
    }
    if (message.type === 'notifications') {
      this.notifications = Array.isArray(message.notifications)
        ? message.notifications
        : [];
      this.unreadNotifications = Number(message.unread || 0);
      return;
    }
    if (message.type === 'settings') {
      this.applyRemoteSettings(
        message.settings && typeof message.settings === 'object'
          ? message.settings
          : {},
      );
    }
  },
};
