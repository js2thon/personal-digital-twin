import fs from "node:fs";
import assert from "node:assert/strict";

const file = "web/ontology-app-studio/index.html";
const html = fs.readFileSync(file, "utf8");
const match = html.match(/<script>([\s\S]*?)<\/script>/);
assert.ok(match, "workbench script block must exist");
new Function(match[1]);

const declared = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map(x => x[1]));
const referenced = new Set([...match[1].matchAll(/\$\('([^']+)'\)/g)].map(x => x[1]));
const missing = [...referenced].filter(id => !declared.has(id));
assert.deepEqual(missing, [], "missing DOM elements: " + missing.join(", "));

for (const endpoint of [
  "/kg/sessions", "/kg/design/draft", "/kg/design/doc", "/kg/design/apply",
  "/kg/import", "/sparql", "/data"
]) assert.ok(html.includes(endpoint), "missing endpoint integration: " + endpoint);

console.log("Ontology App Studio static validation passed.");
