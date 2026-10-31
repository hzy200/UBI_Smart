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
    hljs?: {
      highlightElement: (element: Element) => void;
      getLanguage?: (name: string) => unknown;
      highlight?: (code: string, options: { language: string }) => { value: string };
    };
    mammoth?: {
      convertToHtml: (input: { arrayBuffer: ArrayBuffer }) => Promise<{ value: string }>;
    };
    XLSX?: {
      read: (
        data: ArrayBuffer,
        options: {
          type: 'array';
          cellFormula?: boolean;
          cellNF?: boolean;
        },
      ) => {
        SheetNames: string[];
        Sheets: Record<string, unknown>;
      };
      utils: {
        sheet_to_json: (
          sheet: unknown,
          options: { header: 1; raw: boolean; defval: string },
        ) => unknown[][];
        decode_range: (ref: string) => {
          s: { r: number; c: number };
          e: { r: number; c: number };
        };
        encode_cell: (cell: { r: number; c: number }) => string;
      };
    };
    pdfjsLib?: {
      GlobalWorkerOptions: { workerSrc: string };
      Util: {
        transform: (m1: any[], m2: any[]) => any[];
      };
      getDocument: (input: {
        data: Uint8Array;
        isEvalSupported?: boolean;
      }) => { promise: Promise<any> };
    };
    Alpine?: {
      data: (name: string, factory: (...args: any[]) => unknown) => void;
      initTree: (root: Element) => void;
    };
  }
}
