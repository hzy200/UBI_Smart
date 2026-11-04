import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  libraryDetailRows,
  libraryItemRows,
  libraryPageLabel,
  libraryResultRows,
  libraryStageKey,
  libraryStatusKey,
  libraryStatusLabel,
  libraryUsageText,
} from '../src/ui/library';

const bytes = (value: number) => `${value}B`;

test('libraryStatusLabel maps backend states', () => {
  assert.equal(libraryStatusLabel('ready'), '可检索');
  assert.equal(libraryStatusLabel('failed'), '索引失败');
  assert.equal(libraryStatusLabel('needs_ocr'), '需要 OCR');
  assert.equal(libraryStatusLabel('deleted'), '已删除');
  assert.equal(libraryStatusKey('ready'), 'library.statusReady');
  assert.equal(libraryStatusKey('failed'), 'library.statusFailed');
  assert.equal(libraryStatusKey('processing'), 'library.statusProcessing');
  assert.equal(libraryStageKey('embedding'), 'library.stageEmbedding');
});

test('libraryItemRows formats size, chunks and actions', () => {
  const rows = libraryItemRows(
    [
      {
        item_id: 'lib_a',
        name: '规范.pdf',
        suffix: '.pdf',
        size: 12,
        status: 'ready',
        chunk_count: 3,
        updated_at: '2026-11-02',
      },
      { item_id: 'lib_b', name: '坏文件', status: 'failed', error: 'parse' },
      {
        item_id: 'lib_c',
        name: '处理中',
        status: 'processing',
        job: { job_id: 'job_1', status: 'processing', stage: 'embedding', progress: 65 },
      },
    ],
    bytes,
  );
  assert.equal(rows[0]._status, '可检索');
  assert.match(rows[0]._meta, /PDF/);
  assert.match(rows[0]._meta, /12B/);
  assert.match(rows[0]._meta, /3 块/);
  assert.equal(rows[0]._canReindex, true);
  assert.equal(rows[1]._error, 'parse');
  assert.equal(rows[1]._canDelete, true);
  assert.equal(rows[2]._active, true);
  assert.equal(rows[2]._progress, 65);
  assert.equal(rows[2]._canCancel, true);
});

test('libraryUsageText handles empty and non-empty usage', () => {
  assert.equal(libraryUsageText({ items: 0 }, bytes), '');
  assert.equal(
    libraryUsageText({ items: 2, bytes: 12, max_items: 5, max_total_bytes: 30 }, bytes),
    '2 / 5 份 · 12B / 30B',
  );
});

test('libraryPageLabel and detail rows', () => {
  assert.equal(libraryPageLabel(20, 20, 45), '21-40 / 45');
  assert.equal(libraryPageLabel(0, 20, 0), '0 / 0');
  const rows = libraryDetailRows({
    item: { item_id: 'lib_a', tags: ['规范'] },
    outline: [{ text: '接口', level: 1, anchor: 'heading=1' }],
    chunks: [{ text: '内容', anchor: 'page=1' }],
  });
  assert.equal(rows.outline[0]._label, '接口');
  assert.equal(rows.chunks[0]._source, 'lib_a#page=1');
});

test('libraryResultRows builds stable keys and snippets', () => {
  const rows = libraryResultRows([
    {
      item_id: 'lib_a',
      anchor: 'page=1',
      name: '规范.pdf',
      heading: '接口',
      score: 0.8,
      vector_score: 0.2,
      mode: 'hybrid',
      text: 'x'.repeat(2000),
    },
  ]);
  assert.equal(rows[0]._key, 'lib_a#page=1:0');
  assert.equal(rows[0]._source, 'lib_a#page=1');
  assert.equal(rows[0]._score, '0.800');
  assert.equal(rows[0]._snippet.length, 1200);
});
