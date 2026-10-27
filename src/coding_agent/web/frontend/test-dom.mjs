/* jsdom 交互测试：加载 static 构建产物后交给 Node 内置 test runner。 */

import * as esbuild from "esbuild";
import { mkdtemp, rm } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

// 产物放在项目内，外部依赖 jsdom 才能按 Node 规则解析到 node_modules。
const dir = await mkdtemp(path.join(process.cwd(), ".tmp-dom-test-"));
const outfile = path.join(dir, "dom.test.mjs");

try {
  await esbuild.build({
    entryPoints: ["tests/dom/interaction.test.ts"],
    bundle: true,
    platform: "node",
    format: "esm",
    target: "node20",
    outfile,
    external: ["jsdom"],
    logLevel: "silent",
  });
  await import(pathToFileURL(outfile).href);
} finally {
  await rm(dir, { recursive: true, force: true });
}
