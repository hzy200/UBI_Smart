/* 资料库视图的纯函数：状态、元信息与检索结果格式化。 */

export interface LibraryItemLike {
  item_id?: string;
  name?: string;
  suffix?: string;
  size?: number;
  status?: string;
  chunk_count?: number;
  updated_at?: string;
  error?: string;
  warnings?: string[];
  tags?: string[];
  job?: {
    job_id?: string;
    status?: string;
    stage?: string;
    progress?: number;
    error?: string;
  };
}

export function libraryStatusKey(status: string): string {
  switch (String(status || '')) {
    case 'ready': return 'library.statusReady';
    case 'failed': return 'library.statusFailed';
    case 'needs_ocr': return 'library.statusNeedsOcr';
    case 'deleted': return 'library.statusDeleted';
    case 'queued': return 'library.statusQueued';
    case 'processing': return 'library.statusProcessing';
    case 'cancelled': return 'library.statusCancelled';
    default: return 'library.statusUnknown';
  }
}

export function libraryStageKey(stage: string): string {
  switch (String(stage || '')) {
    case 'queued': return 'library.stageQueued';
    case 'parsing': return 'library.stageParsing';
    case 'chunking': return 'library.stageChunking';
    case 'embedding': return 'library.stageEmbedding';
    case 'writing': return 'library.stageWriting';
    case 'reindexing': return 'library.stageReindexing';
    case 'done': return 'library.stageDone';
    case 'cancelled': return 'library.stageCancelled';
    case 'interrupted': return 'library.stageInterrupted';
    default: return '';
  }
}

export function libraryStatusLabel(status: string): string {
  switch (String(status || '')) {
    case 'ready': return '可检索';
    case 'failed': return '索引失败';
    case 'needs_ocr': return '需要 OCR';
    case 'deleted': return '已删除';
    case 'queued': return '排队中';
    case 'processing': return '索引中';
    case 'cancelled': return '已取消';
    default: return String(status || '未知');
  }
}

export function libraryItemRows(
  items: LibraryItemLike[],
  formatBytes: (n: number) => string,
  statusLabel: (status: string) => string = libraryStatusLabel,
) {
  return (items || []).map((item) => {
    const status = String(item.status || '');
    const chunks = Number(item.chunk_count || 0);
    const suffix = String(item.suffix || '').replace(/^\./, '').toUpperCase();
    const size = formatBytes(Number(item.size || 0));
    const warnings = Array.isArray(item.warnings) ? item.warnings : [];
    const warning = warnings.length ? warnings[0] : '';
    const job = item.job || {};
    const jobStatus = String(job.status || '');
    const active = ['queued', 'processing'].includes(status)
      || ['queued', 'processing'].includes(jobStatus);
    const tags = Array.isArray(item.tags)
      ? item.tags.map((tag) => String(tag)).filter(Boolean)
      : [];
    return {
      ...item,
      _status: statusLabel(status),
      _tags: tags,
      _tagText: tags.join(', '),
      _meta: [suffix, size, chunks ? `${chunks} 块` : '', item.updated_at || '']
        .filter(Boolean)
        .join(' · '),
      _error: String(item.error || job.error || warning || ''),
      _active: active,
      _stage: String(job.stage || ''),
      _progress: Math.max(0, Math.min(100, Number(job.progress || 0))),
      _jobId: String(job.job_id || ''),
      _canCancel: active && Boolean(job.job_id),
      _canReindex: ['ready', 'failed', 'needs_ocr', 'cancelled'].includes(status),
      _canDelete: status !== 'deleted',
    };
  });
}

export function libraryUsageText(
  usage: { items?: number; bytes?: number; max_items?: number; max_total_bytes?: number },
  formatBytes: (n: number) => string,
): string {
  if (!usage || !usage.items) return '';
  return `${usage.items} / ${usage.max_items || '?'} 份 · `
    + `${formatBytes(Number(usage.bytes || 0))} / ${formatBytes(Number(usage.max_total_bytes || 0))}`;
}

export function libraryPageLabel(
  offset: number,
  limit: number,
  total: number,
): string {
  if (!total) return '0 / 0';
  const start = Math.max(0, Number(offset) || 0) + 1;
  const end = Math.min(total, start - 1 + Math.max(1, Number(limit) || 1));
  return `${start}-${end} / ${total}`;
}

export function libraryDetailRows(detail: any) {
  const outline = Array.isArray(detail && detail.outline)
    ? detail.outline.map((item: any, index: number) => ({
      ...item,
      _key: `${item.anchor || ''}:${index}`,
      _label: `${'  '.repeat(Math.max(0, Number(item.level || 1) - 1))}${item.text || ''}`,
    }))
    : [];
  const chunks = Array.isArray(detail && detail.chunks)
    ? detail.chunks.map((chunk: any, index: number) => ({
      ...chunk,
      _key: `${chunk.anchor || ''}:${index}`,
      _source: `${(detail.item && detail.item.item_id) || ''}#${chunk.anchor || ''}`.replace(/#$/, ''),
    }))
    : [];
  return { outline, chunks };
}

export function libraryResultRows(hits: any[]) {
  return (hits || []).map((hit, index) => {
    const source = `${hit.item_id || ''}#${hit.anchor || ''}`.replace(/#$/, '');
    const title = [hit.name || '', hit.heading || ''].filter(Boolean).join(' / ');
    return {
      ...hit,
      _key: `${source}:${hit.ordinal || index}`,
      _source: source,
      _title: title,
      _score: Number(hit.score || 0).toFixed(3),
      _vectorScore: Number(hit.vector_score || 0).toFixed(3),
      _mode: String(hit.mode || ''),
      _snippet: String(hit.text || '').slice(0, 1200),
    };
  });
}
