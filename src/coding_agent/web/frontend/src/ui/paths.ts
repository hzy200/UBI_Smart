export function isWslPath(value: unknown): boolean {
  return String(value || '').trim().startsWith('/');
}

export function isWindowsPath(value: unknown): boolean {
  const text = String(value || '').trim();
  return /^[A-Za-z]:[\\/]/.test(text) || text.startsWith('\\\\');
}
