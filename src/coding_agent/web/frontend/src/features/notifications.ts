/* Alpine mixin: notifications。
 *
 * 站内通知历史与未读数。只保存最近 100 条；持久化到 localStorage，
 * 点击通知可回到对应会话或文件。
 */

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
          id: Number(item.id) || ++this._notificationSeq,
          kind: String(item.kind || 'info'),
          title: String(item.title || ''),
          body: String(item.body || ''),
          time: String(item.time || ''),
          _sessionId: String(item._sessionId || ''),
          _path: String(item._path || ''),
          _clickable: Boolean(item._sessionId || item._path),
          _cls: `notif-${item.kind || 'info'}`,
        }));
      this._notificationSeq = this.notifications.reduce(
        (max, item) => Math.max(max, Number(item.id) || 0),
        0,
      );
      this.unreadNotifications = 0;
    } catch (err) {
      this.notifications = [];
    }
  },

  persistNotifications() {
    try {
      const rows = this.notifications.slice(0, 50).map((item) => ({
        id: item.id,
        kind: item.kind,
        title: item.title,
        body: item.body,
        time: item.time,
        _sessionId: item._sessionId || '',
        _path: item._path || '',
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
  },

  openNotification(event) {
    const id = Number(event.currentTarget.dataset.id);
    const item = this.notifications.find((row) => row.id === id);
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
  },

  clearNotifications() {
    this.notifications = [];
    this.unreadNotifications = 0;
    this.persistNotifications();
  },

  notificationClass(item) {
    return `notif-${item && item.kind ? item.kind : 'info'}`;
  },
};
