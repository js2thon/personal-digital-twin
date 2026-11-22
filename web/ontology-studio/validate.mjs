import fs from "node:fs";
import vm from "node:vm";
const html = fs.readFileSync(new URL("./index.html", import.meta.url), "utf8");
const scripts = [...html.matchAll(/<script>([\\s\\S]*?)<\\/script>/gi)].map(x => x[1]);
if (!scripts.length) throw new Error("No inline application script found");
for (const source of scripts) new vm.Script(source, { filename: "ontology-studio-inline.js" });
const ids = new Set([...html.matchAll(/\\bid="([^"]+)"/g)].map(x => x[1]));
const refs = new Set([...html.matchAll(/\\$\\(["']([^"']+)["']\\)/g)].map(x => x[1]));
const missing = [...refs].filter(x => !ids.has(x));
if (missing.length) throw new Error("Missing DOM ids: " + missing.join(", "));
for (const endpoint of ["/kg/sessions", "/automodel/jobs", "/ontology/metrics", "/automodel/jobs/"]) {
  if (!html.includes(endpoint)) throw new Error("Missing expected API integration: " + endpoint);
}
console.log("Ontology Studio V2 static validation passed");
