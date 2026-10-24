// SSE 帧解析与消费；seq 去重依赖调用方提供的 lastSeq。
// 独立成模块，流式视图不再各自复制解析逻辑。

export interface SseFrame {
  event: any;
  seq: number | null;
}

export interface SseOptions {
  getLastSeq?: () => number;
  onOpen?: () => void;
}

function parseFrame(frame: string): SseFrame | null {
  let seq: number | null = null;
  const dataLines: string[] = [];
  for (const line of frame.split('\n')) {
    if (line.startsWith('id:')) {
      const parsed = Number.parseInt(line.slice(3).trim(), 10);
      if (Number.isFinite(parsed)) seq = parsed;
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice(5).replace(/^\s/, ''));
    }
  }
  const payload = dataLines.join('\n').trim();
  if (!payload) return null;
  try {
    return { event: JSON.parse(payload), seq };
  } catch (err) {
    return null;
  }
}

export async function consumeSse(
  response: Response,
  onFrame: (frame: SseFrame) => void,
  options: SseOptions = {},
): Promise<void> {
  if (!response.ok) {
    let detail = '';
    try { detail = await response.text(); } catch (err) { /* ignore */ }
    throw new Error(detail || `HTTP ${response.status}`);
  }
  if (!response.body) throw new Error('响应没有可读事件流');

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  if (options.onOpen) options.onOpen();

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split('\n\n');
    buffer = frames.pop() ?? '';
    for (const frame of frames) {
      const parsed = parseFrame(frame);
      if (!parsed) continue;
      const last = options.getLastSeq ? options.getLastSeq() : -1;
      if (parsed.seq !== null && parsed.seq <= last) continue;
      onFrame(parsed);
    }
  }
}
