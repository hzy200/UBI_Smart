import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  isMarkdownContinuation,
  renderMarkdownIncremental,
  splitMarkdownTail,
} from '../src/ui/markdown';

test('stable markdown blocks become head and the tail keeps the rest', () => {
  const text = 'first paragraph\n\nsecond paragraph';
  const { head, tail } = splitMarkdownTail(text);
  assert.equal(head, 'first paragraph\n\n');
  assert.equal(tail, 'second paragraph');
});

test('open code fences are never split', () => {
  const text = 'intro\n\n```py\nprint(1)\n\nprint(2)';
  const { head } = splitMarkdownTail(text);
  assert.equal(head, 'intro\n\n');
  const closed = 'intro\n\n```py\nprint(1)\n```\n\noutro';
  assert.equal(splitMarkdownTail(closed).head, 'intro\n\n```py\nprint(1)\n```\n\n');
});

test('list, quote, table and indented continuations stay in the tail', () => {
  for (const next of ['- second', '1. second', '> quote', '| cell |', '    code']) {
    assert.equal(isMarkdownContinuation(next), true, next);
    const { head, tail } = splitMarkdownTail(`- first\n\n${next}`);
    assert.equal(head, '', next);
    assert.equal(tail, `- first\n\n${next}`, next);
  }
  assert.equal(isMarkdownContinuation('plain paragraph'), false);
});

test('incremental rendering reuses cached head and only renders new blocks', () => {
  const cache = { head: '', html: '' };
  const rendered: string[] = [];
  const render = (value: string) => {
    rendered.push(value);
    return `<b>${value.trim()}</b>`;
  };

  const first = renderMarkdownIncremental('one', cache, render);
  assert.equal(first, '<b>one</b>');
  assert.deepEqual(rendered, ['one']);

  rendered.length = 0;
  const second = renderMarkdownIncremental('one\n\n', cache, render);
  assert.equal(second, '<b>one</b>');
  assert.deepEqual(rendered, ['one\n\n']);

  rendered.length = 0;
  const third = renderMarkdownIncremental('one\n\ntwo', cache, render);
  assert.equal(third, '<b>one</b><b>two</b>');
  assert.deepEqual(rendered, ['two']);

  rendered.length = 0;
  const fourth = renderMarkdownIncremental('one\n\ntwo\n\nthree', cache, render);
  assert.equal(fourth, '<b>one</b><b>two</b><b>three</b>');
  assert.deepEqual(rendered, ['two\n\n', 'three']);
});

test('incremental rendering falls back when the stable prefix shrinks', () => {
  const cache = { head: 'alpha\n\n', html: '<b>alpha</b>' };
  const calls: string[] = [];
  const out = renderMarkdownIncremental('beta', cache, (value) => {
    calls.push(value);
    return `<i>${value}</i>`;
  });
  assert.equal(out, '<i>beta</i>');
  assert.deepEqual(calls, ['beta']);
  assert.equal(cache.head, '');
});
