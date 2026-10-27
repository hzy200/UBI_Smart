/* 统一 diff 的纯解析、split 视图与词级 diff；供视图与单元测试共用。 */

export interface DiffRow {
  _key: string;
  text: string;
  _cls: string;
  _no: string;
  _sign: string;
  _code: string;
  _kind: string;
  _html?: string;
}

export interface SplitDiffRow {
  _key: string;
  _cls: string;
  _leftClass: string;
  _rightClass: string;
  _leftNo: string;
  _rightNo: string;
  _leftSign: string;
  _rightSign: string;
  _leftHtml: string;
  _rightHtml: string;
  _hasRight: boolean;
  _full: boolean;
}

export function diffRowsFromText(text: string): DiffRow[] {
  const source = String(text || '');
  if (!source) return [];
  let oldLine = 0;
  let newLine = 0;
  const rows: DiffRow[] = [];
  for (const raw of source.split('\n')) {
    let cls = 'diff-line';
    let no = '';
    let sign = '';
    let code = raw;
    let kind = 'meta';
    if (raw.startsWith('+++') || raw.startsWith('---')) {
      cls += ' diff-file';
      kind = 'file';
    } else if (raw.startsWith('@@')) {
      cls += ' diff-hunk';
      kind = 'hunk';
      const match = raw.match(/@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
      if (match) {
        oldLine = Number(match[1]);
        newLine = Number(match[2]);
      }
    } else if (raw.startsWith('+')) {
      cls += ' diff-add';
      no = `+${newLine}`;
      newLine += 1;
      sign = '+';
      code = raw.slice(1);
      kind = 'add';
    } else if (raw.startsWith('-')) {
      cls += ' diff-del';
      no = `-${oldLine}`;
      oldLine += 1;
      sign = '-';
      code = raw.slice(1);
      kind = 'del';
    } else if (raw.startsWith(' ')) {
      cls += ' diff-ctx';
      no = String(newLine);
      newLine += 1;
      oldLine += 1;
      sign = ' ';
      code = raw.slice(1);
      kind = 'ctx';
    } else {
      cls += ' diff-meta';
    }
    rows.push({
      _key: `${rows.length}:${raw}`,
      text: raw,
      _cls: cls,
      _no: no,
      _sign: sign,
      _code: code,
      _kind: kind,
    });
  }
  return rows;
}

export function splitDiffRows(rows: DiffRow[]): SplitDiffRow[] {
  const result: SplitDiffRow[] = [];
  let index = 0;
  while (index < rows.length) {
    const row = rows[index];
    if (row._kind === 'del' || row._kind === 'add') {
      const dels: DiffRow[] = [];
      const adds: DiffRow[] = [];
      while (index < rows.length && rows[index]._kind === 'del') {
        dels.push(rows[index]);
        index += 1;
      }
      while (index < rows.length && rows[index]._kind === 'add') {
        adds.push(rows[index]);
        index += 1;
      }
      const count = Math.max(dels.length, adds.length);
      for (let i = 0; i < count; i += 1) {
        const left = dels[i];
        const right = adds[i];
        result.push({
          _key: `${result.length}:${left ? left._key : ''}:${right ? right._key : ''}`,
          _cls: 'diff-split-row',
          _leftClass: left
            ? 'diff-split-cell diff-del'
            : 'diff-split-cell diff-empty',
          _rightClass: right
            ? 'diff-split-cell diff-add'
            : 'diff-split-cell diff-empty',
          _leftNo: left ? left._no : '',
          _rightNo: right ? right._no : '',
          _leftSign: left ? left._sign : '',
          _rightSign: right ? right._sign : '',
          _leftHtml: left ? (left._html || '') : '',
          _rightHtml: right ? (right._html || '') : '',
          _hasRight: Boolean(right),
          _full: false,
        });
      }
      continue;
    }
    if (row._kind === 'ctx') {
      result.push({
        _key: row._key,
        _cls: 'diff-split-row',
        _leftClass: 'diff-split-cell diff-ctx',
        _rightClass: 'diff-split-cell diff-ctx',
        _leftNo: row._no,
        _rightNo: row._no,
        _leftSign: ' ',
        _rightSign: ' ',
        _leftHtml: row._html || '',
        _rightHtml: row._html || '',
        _hasRight: true,
        _full: false,
      });
    } else {
      result.push({
        _key: row._key,
        _cls: 'diff-split-row diff-split-full',
        _leftClass: 'diff-split-cell diff-split-full',
        _rightClass: 'diff-split-cell',
        _leftNo: '',
        _rightNo: '',
        _leftSign: '',
        _rightSign: '',
        _leftHtml: row._html || '',
        _rightHtml: '',
        _hasRight: false,
        _full: true,
      });
    }
    index += 1;
  }
  return result;
}

function tokenize(text: string): string[] {
  return String(text || '').match(/\s+|[A-Za-z0-9_]+|[^\sA-Za-z0-9_]/g) || [];
}

function markTokens(
  tokens: string[],
  keep: boolean[],
  open: string,
  close: string,
): string {
  let output = '';
  let marked = false;
  for (let index = 0; index < tokens.length; index += 1) {
    const changed = !keep[index];
    if (changed && !marked) {
      output += open;
      marked = true;
    } else if (!changed && marked) {
      output += close;
      marked = false;
    }
    output += tokens[index];
  }
  if (marked) output += close;
  return output;
}

/** 返回带 \\u0001/\\u0002 标记的行文本；调用方在高亮/转义后替换为 mark。 */
export function wordDiffMarkers(oldText: string, newText: string) {
  const left = tokenize(oldText);
  const right = tokenize(newText);
  const n = left.length;
  const m = right.length;
  if (n * m > 250_000) {
    return { old: String(oldText || ''), new: String(newText || '') };
  }
  const dp: number[][] = Array.from(
    { length: n + 1 },
    () => new Array<number>(m + 1).fill(0),
  );
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      dp[i][j] = left[i] === right[j]
        ? dp[i + 1][j + 1] + 1
        : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const keepLeft = new Array<boolean>(n).fill(false);
  const keepRight = new Array<boolean>(m).fill(false);
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (left[i] === right[j]) {
      keepLeft[i] = true;
      keepRight[j] = true;
      i += 1;
      j += 1;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      i += 1;
    } else {
      j += 1;
    }
  }
  return {
    old: markTokens(left, keepLeft, '\u0001', '\u0002'),
    new: markTokens(right, keepRight, '\u0001', '\u0002'),
  };
}
