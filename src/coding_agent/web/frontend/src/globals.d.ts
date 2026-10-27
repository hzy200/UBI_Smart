// 本地 vendor 与 pywebview 注入的全局对象；只声明前端实际用到的成员。

export {};

declare global {
  interface Window {
    marked?: { parse: (text: string) => string };
    DOMPurify?: {
      sanitize: (html: string, options?: Record<string, unknown>) => string;
    };
    pywebview?: { api: Record<string, (...args: any[]) => any> };
    __APP_CONFIG__?: Record<string, unknown>;
    hljs?: { highlightElement: (element: Element) => void };
    Alpine?: {
      data: (name: string, factory: (...args: any[]) => unknown) => void;
      initTree: (root: Element) => void;
    };
  }
}
