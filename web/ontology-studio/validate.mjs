import fs from "node:fs";
import vm from "node:vm";
const html = fs.readFileSync(new URL("./index.html", import.meta.url), "utf8");
const open = html.indexOf("<script>");
const close = html.lastIndexOf("</script>");
if (open < 0 || close <= open) throw new Error("No inline application script found");
const source = html.slice(open + "<script>".length, close);
new vm.Script(source, { filename: "ontology-studio-inline.js" });
const ids = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map(x => x[1]));
const refs = new Set([...source.matchAll(/\$\(["']([^"']+)["']\)/g)].map(x => x[1]));
const missing = [...refs].filter(x => !ids.has(x));
if (missing.length) throw new Error("Missing DOM ids: " + missing.join(", "));
for (const endpoint of ["/kg/sessions", "/automodel/jobs", "/ontology/metrics", "/studio/schema-graph", "/studio/quality", "/pipeline"]) {
  if (!html.includes(endpoint)) throw new Error("Missing expected API integration: " + endpoint);
}
console.log("Ontology Studio V2 static validation passed");
