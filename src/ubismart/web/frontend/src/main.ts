import "./app";
import type { AgentEvent } from "./contracts";

// 保留类型入口，后续拆分 app.ts 时逐步把匿名对象换成显式状态/事件类型。
export type { AgentEvent };
