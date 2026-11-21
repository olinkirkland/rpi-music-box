// Compile the page's Vue templates (index.html and every `template:` in the
// JS files given) with Vue's own compiler, and report anything wrong.
// Run through tools/check-page.sh, which provides the compiler.
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";

const [modules, html, ...jsFiles] = process.argv.slice(2);
const require = createRequire(modules + "/");
const { compile } = require("@vue/compiler-dom");

const templates = [];
const page = readFileSync(html, "utf8");
const start = page.indexOf('id="app"');
const open = page.lastIndexOf("<", start);
templates.push([html, page.slice(page.indexOf(">", open) + 1, page.lastIndexOf("</main>"))]);
for (const file of jsFiles) {
  for (const [, t] of readFileSync(file, "utf8").matchAll(/template:\s*`([\s\S]*?)`/g)) templates.push([file, t]);
}
let problems = 0;
for (const [where, t] of templates) {
  const errors = [];
  compile(t, { onError: (e) => errors.push(e.message), onWarn: (e) => errors.push("warn: " + e.message) });
  if (errors.length) console.log(where + ": " + errors.join("; "));
  problems += errors.length;
}
console.log(`${templates.length} templates checked, ${problems} problem(s)`);
process.exit(problems ? 1 : 0);
