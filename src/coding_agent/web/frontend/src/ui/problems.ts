/* 把平铺的 problems 聚合成「文件」或「来源」组。 */

export const PROBLEM_KIND_LABELS: Record<string, string> = {
  verification: '验证',
  review: '审查',
  run: '运行',
};

export interface ProblemGroup {
  _key: string;
  _label: string;
  _path: string;
  _items: any[];
  _count: number;
  _errors: number;
  _warnings: number;
  _open: boolean;
  _toggle: string;
  _countText: string;
  _cls: string;
}

export function problemGroupKey(problem: any): string {
  const path = String((problem && problem._path) || '').trim();
  if (path) return `file:${path}`;
  return `kind:${(problem && problem.kind) || 'other'}`;
}

export function groupProblems(
  problems: any[],
  expanded: Record<string, boolean> = {},
): ProblemGroup[] {
  const order: string[] = [];
  const groups = new Map<string, any>();
  for (const problem of problems || []) {
    const key = problemGroupKey(problem);
    let group = groups.get(key);
    if (!group) {
      const path = String((problem && problem._path) || '');
      const kind = String((problem && problem.kind) || '');
      group = {
        _key: key,
        _label: path || PROBLEM_KIND_LABELS[kind] || kind || '其他',
        _path: path,
        _items: [],
        _count: 0,
        _errors: 0,
        _warnings: 0,
      };
      groups.set(key, group);
      order.push(key);
    }
    group._items.push({ ...problem, _itemCls: (problem && problem._cls) || '' });
    group._count += 1;
    if ((problem && problem.severity) === 'error') group._errors += 1;
    else group._warnings += 1;
  }
  return order.map((key) => {
    const group = groups.get(key);
    const open = expanded[key] !== false;
    const detail: string[] = [];
    if (group._errors) detail.push(`${group._errors} 错误`);
    if (group._warnings) detail.push(`${group._warnings} 警告`);
    return {
      ...group,
      _open: open,
      _toggle: open ? '▾' : '▸',
      _countText: detail.join(' / ') || `${group._count} 项`,
      _cls: `problem-group${group._errors ? ' has-error' : ''}`,
    };
  });
}
