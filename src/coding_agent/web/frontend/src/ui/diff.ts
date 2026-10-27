/* 统一 diff 的纯解析逻辑；供视图与单元测试共用。 */

export interface DiffRow {
  _key: string;
  text: string;
  _cls: string;
  _no: string;
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
    if (raw.startsWith('+++') || raw.startsWith('---')) {
      cls += ' diff-file';
    } else if (raw.startsWith('@@')) {
      cls += ' diff-hunk';
      const match = raw.match(/@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
      if (match) {
        oldLine = Number(match[1]);
        newLine = Number(match[2]);
      }
    } else if (raw.startsWith('+')) {
      cls += ' diff-add';
      no = `+${newLine}`;
      newLine += 1;
    } else if (raw.startsWith('-')) {
      cls += ' diff-del';
      no = `-${oldLine}`;
      oldLine += 1;
    } else if (raw.startsWith(' ')) {
      cls += ' diff-ctx';
      no = String(newLine);
      newLine += 1;
      oldLine += 1;
    } else {
      cls += ' diff-meta';
    }
    rows.push({
      _key: `${rows.length}:${raw}`,
      text: raw,
      _cls: cls,
      _no: no,
    });
  }
  return rows;
}
