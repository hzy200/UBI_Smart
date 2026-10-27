/* 纯函数单元测试：esbuild 打包到临时文件后交给 Node 内置 test runner。 */

import * as esbuild from "esbuild";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

const dir = await mkdtemp(path.join(tmpdir(), "codingagent-fe-tests-"));
const outfile = path.join(dir, "tests.mjs");

try {
  await esbuild.build({
    entryPoints: ["tests/all.test.ts"],
    bundle: true,
    platform: "node",
    format: "esm",
    target: "node20",
    outfile,
    external: ["node:test", "node:assert", "node:assert/strict"],
    logLevel: "silent",
  });
  await import(pathToFileURL(outfile).href);
} finally {
  await rm(dir, { recursive: true, force: true });
}
