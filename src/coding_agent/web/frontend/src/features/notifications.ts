/* Alpine mixin: notifications。
 *
 * 站内通知历史与未读数。只保存最近 100 条，不持久化敏感内容。
 */

const MAX_NOTIFICATIONS = 100;

export const notificationsMixin = {
  pushNotification(kind, title, body = '') {
    const text = String(title || '').trim();
    if (!text) return;
    const item = {
      id: ++this._notificationSeq,
      kind: kind || 'info',
      title: text,
      body: String(body || '').slice(0, 300),
      time: new Date().toLocaleTimeString(),
      _cls: `notif-${kind || 'info'}`,
    };
    this.notifications = [item, ...this.notifications].slice(0, MAX_NOTIFICATIONS);
    if (this.notificationsOpen) this.unreadNotifications = 0;
    else this.unreadNotifications += 1;
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
  },

  notificationClass(item) {
    return `notif-${item && item.kind ? item.kind : 'info'}`;
  },
};
