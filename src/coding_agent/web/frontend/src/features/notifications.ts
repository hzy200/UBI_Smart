/* Alpine mixin: notifications。
 *
 * 站内通知历史与未读数。服务端控制面可用时以服务端为事实源并通过 SSE
 * 推送；localStorage 只做离线缓存（最近 100 条）。点击通知可回到会话或文件。
 */

import { authFetch, readJson } from '../api/http';
import { consumeSse } from '../sse';

const MAX_NOTIFICATIONS = 100;
const STORAGE_KEY = 'codingagent.notifications';

export const notificationsMixin = {
  loadNotifications() {
    try {
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');
      if (!Array.isArray(saved)) return;
      this.notifications = saved
        .filter((item) => item && item.title)
        .slice(0, MAX_NOTIFICATIONS)
        .map((item) => ({
          id: item.id ?? ++this._notificationSeq,
          kind: String(item.kind || 'info'),
          title: String(item.title || ''),
          body: String(item.body || ''),
          time: String(item.time || ''),
          read: Boolean(item.read),
          _sessionId: String(item._sessionId || ''),
          _path: String(item._path || ''),
          _clickable: Boolean(item._sessionId || item._path),
          _cls: `notif-${item.kind || 'info'}`,
          _server: Boolean(item._server),
        }));
      const numeric = this.notifications
        .map((item) => Number(item.id))
        .filter((value) => Number.isFinite(value));
      this._notificationSeq = numeric.length ? Math.max(...numeric) : 0;
      this.unreadNotifications = 0;
    } catch (err) {
      this.notifications = [];
    }
  },

  persistNotifications() {
    try {
      const rows = this.notifications.slice(0, MAX_NOTIFICATIONS).map((item) => ({
        id: item.id,
        kind: item.kind,
        title: item.title,
        body: item.body,
        time: item.time,
        read: Boolean(item.read),
        _sessionId: item._sessionId || '',
        _path: item._path || '',
        _server: Boolean(item._server),
      }));
      localStorage.setItem(STORAGE_KEY, JSON.stringify(rows));
    } catch (err) { /* ignore */ }
  },

  pushNotification(kind, title, body = '', meta: any = {}) {
    const text = String(title || '').trim();
    if (!text) return;
    const item = {
      id: ++this._notificationSeq,
      kind: kind || 'info',
      title: text,
      body: String(body || '').slice(0, 300),
      time: new Date().toLocaleTimeString(),
      _sessionId: String(meta.sessionId || ''),
      _path: String(meta.path || ''),
      _clickable: Boolean(meta.sessionId || meta.path),
      _cls: `notif-${kind || 'info'}`,
    };
    this.notifications = [item, ...this.notifications].slice(0, MAX_NOTIFICATIONS);
    if (this.notificationsOpen) this.unreadNotifications = 0;
    else this.unreadNotifications += 1;
    this.persistNotifications();
    this.broadcastNotifications();
  },

  broadcastNotifications() {
    this.broadcastSync('notifications', {
      notifications: this.notifications,
      unread: this.unreadNotifications,
    });
  },

  notificationTime(value) {
    const text = String(value || '');
    if (!text) return '';
    const parsed = Date.parse(text);
    if (!Number.isFinite(parsed)) return text.slice(0, 16).replace('T', ' ');
    return new Date(parsed).toLocaleTimeString();
  },

  serverNotificationItem(item) {
    const threadId = String((item && item.thread_id) || '');
    const path = String((item && item.path) || '');
    const kind = String((item && item.kind) || 'info');
    return {
      id: String((item && item.id) || ''),
      kind,
      title: String((item && item.title) || ''),
      body: String((item && item.body) || ''),
      time: this.notificationTime(item && item.created_at),
      read: Boolean(item && item.read),
      _sessionId: threadId,
      _path: path,
      _clickable: Boolean(threadId || path),
      _cls: `notif-${kind}`,
      _server: true,
    };
  },

  /** 服务端快照是事实源；本地即时通知会被同一条服务端记录替换。 */
  applyServerNotifications(data) {
    if (!data || data.enabled === false) {
      this.notificationsServer = false;
      this._notificationStreamDisabled = true;
      return;
    }
    this.notificationsServer = true;
    this.notifications = (
      Array.isArray(data.notifications) ? data.notifications : []
    )
      .map((item) => this.serverNotificationItem(item))
      .slice(0, MAX_NOTIFICATIONS);
    this.unreadNotifications = Number(data.unread || 0);
    this.persistNotifications();
  },

  async loadServerNotifications() {
    try {
      const response = await authFetch('/api/notifications?limit=100');
      const data = await readJson(response, '读取通知失败');
      this.applyServerNotifications(data);
    } catch (err) {
      // 控制面不可用：继续使用本地通知。
      this.notificationsServer = false;
    }
  },

  async connectNotificationStream() {
    if (this._notificationStreamDisabled || this._notificationStreamAbort) {
      return;
    }
    const controller = new AbortController();
    this._notificationStreamAbort = controller;
    try {
      const response = await authFetch('/api/notifications/stream', {
        signal: controller.signal,
      });
      await consumeSse(response, (frame) => {
        if (frame.event && typeof frame.event === 'object') {
          this.applyServerNotifications(frame.event);
        }
      });
    } catch (err) {
      // 断线后由下方定时器重连；不再额外提示，避免打扰用户。
    } finally {
      if (this._notificationStreamAbort === controller) {
        this._notificationStreamAbort = null;
      }
      if (!this._notificationStreamDisabled) {
        this._notificationRetry = setTimeout(
          () => void this.connectNotificationStream(),
          30000,
        );
      }
    }
  },

  urlBase64ToUint8Array(value) {
    const padding = '='.repeat((4 - (String(value).length % 4)) % 4);
    const base64 = (String(value) + padding)
      .replace(/-/g, '+')
      .replace(/_/g, '/');
    const raw = atob(base64);
    const output = new Uint8Array(raw.length);
    for (let index = 0; index < raw.length; index += 1) {
      output[index] = raw.charCodeAt(index);
    }
    return output;
  },

  async syncPushSubscription(subscription) {
    const payload = subscription && subscription.toJSON
      ? subscription.toJSON()
      : subscription;
    if (!payload || !payload.endpoint) return;
    const response = await authFetch('/api/push/subscriptions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        endpoint: payload.endpoint,
        keys: {
          p256dh: (payload.keys && payload.keys.p256dh) || '',
          auth: (payload.keys && payload.keys.auth) || '',
        },
        user_agent: String(navigator.userAgent || '').slice(0, 250),
      }),
    });
    await readJson(response, '保存推送订阅失败');
  },

  /** 用户已授予通知权限时订阅 Web Push；否则等用户在设置里开启。 */
  async setupPush() {
    if (
      typeof navigator === 'undefined'
      || !('serviceWorker' in navigator)
      || !('PushManager' in window)
      || !('Notification' in window)
      || Notification.permission !== 'granted'
    ) {
      return;
    }
    try {
      const response = await authFetch('/api/push/config');
      const data = await readJson(response, '读取推送配置失败');
      if (!data.enabled || !data.public_key) return;
      const registration = await navigator.serviceWorker.ready;
      let subscription = await registration.pushManager.getSubscription();
      if (!subscription) {
        subscription = await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: this.urlBase64ToUint8Array(
            data.public_key,
          ),
        });
      }
      await this.syncPushSubscription(subscription);
    } catch (err) {
      // 推送是增强能力；失败不影响站内通知。
    }
  },

  async removePushSubscription() {
    if (
      typeof navigator === 'undefined'
      || !('serviceWorker' in navigator)
      || !('PushManager' in window)
    ) {
      return;
    }
    try {
      const registration = await navigator.serviceWorker.ready;
      const subscription = await registration.pushManager.getSubscription();
      if (!subscription) return;
      const payload = subscription.toJSON
        ? subscription.toJSON()
        : subscription;
      if (payload && payload.endpoint) {
        await authFetch('/api/push/subscriptions', {
          method: 'DELETE',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ endpoint: payload.endpoint }),
        });
      }
      await subscription.unsubscribe();
    } catch (err) {
      // 注销失败不影响本地登出。
    }
  },

  async syncNotificationRead(payload) {
    try {
      const response = await authFetch('/api/notifications/read', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      await readJson(response, '同步通知已读失败');
    } catch (err) {
      // 下一次服务端快照会纠正已读状态。
    }
  },

  async syncNotificationClear() {
    try {
      const response = await authFetch('/api/notifications', {
        method: 'DELETE',
      });
      await readJson(response, '清空通知失败');
    } catch (err) {
      // 下一次服务端快照会纠正列表。
    }
  },

  openNotification(event) {
    const id = String(event.currentTarget.dataset.id || '');
    const item = this.notifications.find(
      (row) => String(row.id) === id,
    );
    this.closeNotifications();
    if (!item) return;
    if (item._sessionId) {
      this.openSession(item._sessionId);
      return;
    }
    if (item._path) this.openFile(item._path);
  },

  toggleNotifications() {
    this.notificationsOpen = !this.notificationsOpen;
    if (this.notificationsOpen) this.markNotificationsRead();
  },

  closeNotifications() {
    this.notificationsOpen = false;
  },

  markNotificationsRead() {
    this.unreadNotifications = 0;
    this.notifications = this.notifications.map((item) => ({
      ...item,
      read: true,
    }));
    this.persistNotifications();
    this.broadcastNotifications();
    if (this.notificationsServer) {
      void this.syncNotificationRead({ all: true });
    }
  },

  clearNotifications() {
    this.notifications = [];
    this.unreadNotifications = 0;
    this.persistNotifications();
    this.broadcastNotifications();
    if (this.notificationsServer) void this.syncNotificationClear();
  },

  notificationClass(item) {
    return `notif-${item && item.kind ? item.kind : 'info'}`;
  },
};
