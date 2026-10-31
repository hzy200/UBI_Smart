/* Git 状态 → 文件树标记的纯函数。 */

export type GitStatusMap = Record<string, string>;

const GIT_LABELS: Record<string, string> = {
  M: '已修改',
  A: '新增',
  D: '已删除',
  R: '重命名',
  C: '复制',
  U: '未跟踪',
};

const EMPTY = { _git: '', _gitCls: 'tree-git', _gitTitle: '' };

/** porcelain 状态可能是双字符（如 `MM`、`??`），归一成单个标记字母。 */
export function normalizeGitStatus(status: unknown): string {
  const value = String(status || '').trim().toUpperCase();
  if (!value) return '';
  if (value.includes('?')) return 'U';
  if (value.includes('D')) return 'D';
  if (value.includes('A')) return 'A';
  if (value.includes('R')) return 'R';
  if (value.includes('C')) return 'C';
  if (value.includes('M')) return 'M';
  return '';
}

export function buildGitStatusMap(changes: unknown): GitStatusMap {
  const map: GitStatusMap = {};
  for (const row of (changes as any[]) || []) {
    const path = String((row && row.path) || '').replace(/\\/g, '/');
    const letter = normalizeGitStatus(row && row.status);
    if (path && letter) map[path] = letter;
  }
  return map;
}

/** 给文件树行附加 `_git` / `_gitCls` / `_gitTitle`；目录显示聚合圆点。 */
export function gitTreeFields(map: GitStatusMap, path: string, isDir: boolean) {
  const statuses = map || {};
  if (isDir) {
    const prefix = path ? `${path}/` : '';
    const count = Object.keys(statuses).filter(
      (key) => key.startsWith(prefix),
    ).length;
    if (!count) return { ...EMPTY };
    return {
      _git: '●',
      _gitCls: 'tree-git git-dir',
      _gitTitle: `${count} 个未提交改动`,
    };
  }
  const letter = statuses[path] || '';
  if (!letter) return { ...EMPTY };
  return {
    _git: letter,
    _gitCls: `tree-git git-${letter.toLowerCase()}`,
    _gitTitle: GIT_LABELS[letter] || '已修改',
  };
}
