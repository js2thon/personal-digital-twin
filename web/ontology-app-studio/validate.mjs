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
  "/automodel/jobs?limit=100",
  "/studio/overview",
  "/studio/schema-graph",
  "/studio/instance-graph",
  "/studio/objects?cls=",
  "/studio/quality",
  "/studio/governance",
  "/pipeline",
  "/ontology/metrics?limit=200"
]) assert.ok(html.includes(endpoint), "missing backend integration: " + endpoint);
assert.ok(!html.includes("/kg/"), "legacy /kg endpoints must not be used by App Studio");

for (const id of [
  "jobs", "refresh", "reload", "apiBase", "apiKey", "connection",
  "overviewGraph", "schemaGraph", "objectTypes", "linkTypes",
  "classSelect", "instanceSearch", "searchInstances", "instanceTable",
  "instanceGraph", "pipelineStages", "quality", "governance",
  "loadMetrics", "metricTable", "export"
]) assert.ok(declared.has(id), "missing workbench control: " + id);

for (const id of ["refresh", "reload", "searchInstances", "classSelect", "loadMetrics", "export"])
  assert.ok(match[1].includes("$('" + id + "')"), "control is not wired: " + id);

assert.ok(html.includes("同一自动建模任务"), "UI must explain shared task data lineage");
assert.ok(html.includes("../shared/design-system.css"), "shared design system stylesheet must be loaded");
assert.ok(match[1].includes("Promise.allSettled"), "independent API sections must fail independently");
assert.ok(match[1].includes("无法连接后端 API"), "network failures must be surfaced to the user");
assert.ok(!match[1].includes("localStorage"), "server data must not be shadowed by stale local snapshots");

console.log("Ontology App Studio frontend/backend contract validation passed.");
