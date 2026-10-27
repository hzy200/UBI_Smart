/* 非响应式运行时状态。
 *
 * Alpine 的 CSP 构建在数据对象里放 Map 会丢失整个 data scope，
 * 所以这些 Map 放在模块级，不进入 Alpine 的响应式对象。
 */

export const toolMap = new Map<string, any>();
export const renderTimers = new Map<number, any>();
export const tokenBuffers = new Map<number, string>();
