/* 不能经过 Alpine 响应式 Proxy 的第三方对象，放模块级缓存。 */

export const PDF_DOCS = new Map<string, any>();
export const XLSX_BOOKS = new Map<string, any>();
