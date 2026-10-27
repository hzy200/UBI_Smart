/* 非响应式运行时状态。
 *
 * Alpine 的 CSP 构建在数据对象里放 Map 会丢失整个 data scope，
 * 所以这些 Map 放在模块级，不进入 Alpine 的响应式对象。
 */

export const toolMap = new Map<string, any>();
export const renderTimers = new Map<number, any>();
export const tokenBuffers = new Map<number, string>();
// 消息 id -> 实测高度（含消息间距）；虚拟列表的非响应式缓存。
export const messageHeights = new Map<number, number>();
