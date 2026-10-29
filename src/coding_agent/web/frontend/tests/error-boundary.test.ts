import { test } from 'node:test';
import assert from 'node:assert/strict';

import { ErrorBoundary } from '../src/admin/components/ErrorBoundary';

/* 错误边界的两个关键行为在这里验证：
 * 1) 渲染抛错必须转成"记录错误"的状态（否则整棵树被卸载 = 白屏）；
 * 2) 错误必须被上报（控制台/上报回调），不能静默吞掉。
 * 剩下的渲染部分靠 E2E（tests/e2e/admin-error.spec.ts）验证。 */

test('getDerivedStateFromError 把渲染错误转成状态', () => {
  const state = ErrorBoundary.getDerivedStateFromError(new Error('boom'));
  assert.equal(state.error?.message, 'boom');
});

test('初始状态没有错误：正常子树原样渲染', () => {
  const boundary = new ErrorBoundary({ children: null });
  assert.equal(boundary.state.error, null);
  assert.equal(boundary.render(), null);
});

test('componentDidCatch 上报错误并带上边界名', () => {
  const reported: Array<{ error: Error; stack: string }> = [];
  const original = console.error;
  console.error = () => {};
  try {
    const boundary = new ErrorBoundary({
      label: '用户',
      onError: (error, info) => {
        reported.push({ error, stack: info.componentStack });
      },
    });
    boundary.componentDidCatch(new Error('炸了'), { componentStack: 'at Users' });
  } finally {
    console.error = original;
  }
  assert.equal(reported.length, 1);
  assert.equal(reported[0].error.message, '炸了');
  assert.equal(reported[0].stack, 'at Users');
});

test('没有 onError 时也不吞日志：至少落到 console', () => {
  const lines: unknown[][] = [];
  const original = console.error;
  console.error = (...args: unknown[]) => {
    lines.push(args);
  };
  try {
    new ErrorBoundary({}).componentDidCatch(new Error('x'), {
      componentStack: '',
    });
  } finally {
    console.error = original;
  }
  assert.equal(lines.length, 1);
  assert.match(String(lines[0][0]), /admin:root/);
});
