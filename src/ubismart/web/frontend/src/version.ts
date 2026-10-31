/* 前端版本与兼容性判断。
 *
 * 版本握手契约见 docs/rfc/0003-frontend-backend-separation.md：
 * 服务端 /healthz 返回 api_version / min_frontend_version，
 * 前端不兼容时必须阻断，不能带着错误契约继续发请求。
 */

export const FRONTEND_VERSION = '0.1.0';

export class VersionError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = 'VersionError';
    this.code = code;
  }
}

export function parseSemver(value: string): [number, number, number] | null {
  const match = /^v?(\d+)\.(\d+)\.(\d+)$/.exec(String(value || '').trim());
  if (!match) return null;
  return [Number(match[1]), Number(match[2]), Number(match[3])];
}

export function compareSemver(left: string, right: string): number {
  const a = parseSemver(left);
  const b = parseSemver(right);
  if (!a || !b) {
    throw new VersionError(
      'version.invalid_metadata',
      `无法比较版本号：${left} / ${right}`,
    );
  }
  for (let index = 0; index < 3; index += 1) {
    if (a[index] !== b[index]) return a[index] < b[index] ? -1 : 1;
  }
  return 0;
}
