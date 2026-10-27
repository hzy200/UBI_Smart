/* 构建期拼装 HTML 模板与 CSS 分片。
 *
 * 产物仍然是 static/index.html 与 static/style.css，运行期没有任何模板引擎。
 * 分片使用 `{{> name }}` 语法，路径相对 src/templates 或 src/styles。
 */

import { readFile, writeFile, access } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const STATIC = path.resolve(HERE, "..", "static");
const TEMPLATES = path.join(HERE, "src", "templates");
const STYLES = path.join(HERE, "src", "styles");

const INCLUDE = /\{\{>\s*([\w./-]+)\s*\}\}/g;

async function exists(file) {
  try {
    await access(file);
    return true;
  } catch (err) {
    return false;
  }
}

async function render(file, stack = []) {
  if (stack.includes(file)) {
    throw new Error(`模板循环引用：${[...stack, file].join(" -> ")}`);
  }
  const source = await readFile(file, "utf8");
  const parts = [];
  let last = 0;
  for (const match of source.matchAll(INCLUDE)) {
    parts.push(source.slice(last, match.index));
    const target = path.resolve(path.dirname(file), match[1]);
    // 分片自身的尾部空白不参与拼装，由模板里的空行控制节间距。
    parts.push((await render(target, [...stack, file])).replace(/\s+$/, ""));
    last = match.index + match[0].length;
  }
  parts.push(source.slice(last));
  return parts.join("");
}

export async function buildHtml() {
  const template = path.join(TEMPLATES, "index.template.html");
  const html = await render(template);
  await writeFile(path.join(STATIC, "index.html"), html, "utf8");
  return path.join(STATIC, "index.html");
}

export async function buildCss() {
  const template = path.join(STYLES, "index.template.css");
  if (!(await exists(template))) return null;
  const css = await render(template);
  await writeFile(path.join(STATIC, "style.css"), css, "utf8");
  return path.join(STATIC, "style.css");
}

export async function buildUi() {
  const outputs = [];
  outputs.push(await buildHtml());
  const css = await buildCss();
  if (css) outputs.push(css);
  return outputs;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const outputs = await buildUi();
  for (const file of outputs) console.log(`built ${path.relative(HERE, file)}`);
}
