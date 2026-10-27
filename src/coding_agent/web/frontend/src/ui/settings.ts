/* 设置分节关键词搜索。 */

export interface SettingsSection {
  key: string;
  label: string;
  keywords: string[];
}

export const SETTINGS_SECTIONS: SettingsSection[] = [
  {
    key: 'appearance',
    label: '外观',
    keywords: [
      '外观', '主题', '深色', '浅色', '跟随系统', '字体', '字号', '密度',
      '行高', '动效', 'theme', 'font', 'density', 'appearance',
    ],
  },
  {
    key: 'models',
    label: '模型',
    keywords: [
      '模型', '供应商', 'provider', 'profile', 'api key', '密钥',
      '温度', 'temperature', '上下文', 'token', 'model',
    ],
  },
  {
    key: 'workspace',
    label: '工作区',
    keywords: [
      '工作区', '项目', '权限', '只读', '写入', '上传', 'wsl', '本机',
      'workspace', 'project', 'access', 'upload',
    ],
  },
  {
    key: 'diagnostics',
    label: '诊断',
    keywords: [
      '诊断', '日志', '更新', '通知', '健康', '版本', 'diagnostics',
      'log', 'update', 'notification', 'version', 'health',
    ],
  },
  {
    key: 'advanced',
    label: '高级',
    keywords: [
      '高级', '快捷键', '重置', '导入', '导出', '实验', 'advanced',
      'shortcut', 'reset', 'import', 'export', 'experimental',
    ],
  },
];

export function matchesSettingsQuery(
  query: unknown,
  keywords: string[],
): boolean {
  const text = String(query ?? '').trim().toLowerCase();
  if (!text) return true;
  return (keywords || []).some(
    (keyword) => String(keyword).toLowerCase().includes(text),
  );
}

export function settingsVisibility(
  query: unknown,
  sections: SettingsSection[] = SETTINGS_SECTIONS,
): Record<string, boolean> {
  const result: Record<string, boolean> = {};
  for (const section of sections) {
    result[section.key] = matchesSettingsQuery(query, section.keywords);
  }
  return result;
}
