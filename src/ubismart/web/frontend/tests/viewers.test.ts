import { test } from 'node:test';
import assert from 'node:assert/strict';

import { detectViewerKind } from '../src/features/viewer-kind';

test('viewer kind sends images and pdf to dedicated viewers', () => {
  assert.equal(
    detectViewerKind('a.png', { image: true, data_url: 'data:image/png;base64,x' }),
    'image',
  );
  assert.equal(detectViewerKind('a.PDF'), 'pdf');
});

test('viewer kind recognizes office documents', () => {
  for (const path of ['a.docx', 'a.xlsx', 'a.xlsm', 'a.xls', 'a.pptx']) {
    assert.equal(detectViewerKind(path), 'document', path);
  }
});

test('viewer kind rejects legacy office formats with unsupported', () => {
  assert.equal(detectViewerKind('a.doc'), 'unsupported');
  assert.equal(detectViewerKind('a.ppt'), 'unsupported');
  assert.equal(detectViewerKind('a.bin', { binary: true }), 'unsupported');
  assert.equal(
    detectViewerKind('a.png', { image_too_large: true }),
    'unsupported',
  );
});

test('viewer kind renders markdown and falls back to code', () => {
  assert.equal(detectViewerKind('README.md'), 'markdown');
  assert.equal(detectViewerKind('notes.markdown'), 'markdown');
  assert.equal(detectViewerKind('main.py'), 'code');
  assert.equal(detectViewerKind('Dockerfile'), 'code');
});

test('svg is shown as source, never rendered as an image', () => {
  assert.equal(
    detectViewerKind('icon.svg', {
      image: true,
      data_url: 'data:image/svg+xml;base64,x',
    }),
    'code',
  );
});
