/* Alpine mixin: focus。模态打开时把焦点移入，关闭时还给触发元素。 */

import { computeTabTarget, FOCUSABLE_SELECTOR } from '../ui/focus';

function modalVisible(element: Element | null): boolean {
  if (!element) return false;
  if (element.hasAttribute('x-cloak')) return false;
  return (element as HTMLElement).style.display !== 'none';
}

export const focusMixin = {
  activeModal(): HTMLElement | null {
    const nodes = Array.from(document.querySelectorAll('.modal')) as HTMLElement[];
    for (let i = nodes.length - 1; i >= 0; i -= 1) {
      if (modalVisible(nodes[i])) return nodes[i];
    }
    return null;
  },

  modalFocusables(modal: Element | null): HTMLElement[] {
    if (!modal) return [];
    const nodes = Array.from(
      modal.querySelectorAll(FOCUSABLE_SELECTOR),
    ) as HTMLElement[];
    return nodes.filter((node) => {
      if (node.hasAttribute('disabled')) return false;
      // 排除被 x-show 折叠的祖先（例如设置中未激活的分类页）。
      let current: HTMLElement | null = node;
      while (current && current !== modal) {
        if (
          current.hasAttribute('hidden')
          || current.style.display === 'none'
        ) {
          return false;
        }
        current = current.parentElement;
      }
      return true;
    });
  },

  rememberModalFocus() {
    if (this._modalReturnFocus) return;
    const active = document.activeElement;
    this._modalReturnFocus = active && active !== document.body ? active : null;
  },

  restoreModalFocus(remaining = 8) {
    // x-show 的隐藏可能晚于本轮 nextTick；模态还在时稍等再还焦点。
    if (this.activeModal()) {
      if (remaining > 0) {
        setTimeout(() => this.restoreModalFocus(remaining - 1), 16);
      }
      return;
    }
    const target: any = this._modalReturnFocus;
    this._modalReturnFocus = null;
    if (target && target.isConnected && typeof target.focus === 'function') {
      target.focus();
    }
  },

  focusModal(selector: string) {
    this.rememberModalFocus();
    const attempt = (remaining: number) => {
      this.$nextTick(() => {
        const modal = selector
          ? document.querySelector(selector)
          : this.activeModal();
        if (!modal) return;
        // x-show 的 display 更新可能晚于本轮 nextTick，稍等再聚焦。
        if (!modalVisible(modal) && remaining > 0) {
          setTimeout(() => attempt(remaining - 1), 16);
          return;
        }
        const items = this.modalFocusables(modal);
        if (items.length) items[0].focus();
      });
    };
    attempt(5);
  },

  trapModalFocus(event: KeyboardEvent): boolean {
    const modal = this.activeModal();
    if (!modal) return false;
    const items = this.modalFocusables(modal);
    if (!items.length) {
      event.preventDefault();
      return true;
    }
    const activeIndex = items.indexOf(document.activeElement as HTMLElement);
    const target = computeTabTarget(items.length, activeIndex, event.shiftKey);
    event.preventDefault();
    if (target >= 0 && items[target]) items[target].focus();
    return true;
  },
};
