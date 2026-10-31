/* viewer 拆分后的共享类型：inspector 只需提供这些能力。 */

export interface ViewerContext {
  fetchRawBuffer(path: string): Promise<ArrayBuffer>;
  fetchDocument(
    path: string,
    includeImages?: boolean,
    includeLayout?: boolean,
  ): Promise<any>;
  fetchRawUrl(path: string): Promise<string>;
  renderPdfPage(view: any, page: number): Promise<void>;
  formatBytes(size: number): string;
  fileLanguage(path: string): string;
  highlightCode(path: string, text: string, binary: boolean): string;
}

export type FileViewBase = Record<string, any>;
