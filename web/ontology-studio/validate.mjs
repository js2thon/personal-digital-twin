import fs from "node:fs";
import vm from "node:vm";
const html = fs.readFileSync(new URL("./index.html", import.meta.url), "utf8");
const open = html.indexOf("<script>");
const close = html.indexOf("</script>", open);
if (open < 0 || close <= open) throw new Error("No inline application script found");
const source = html.slice(open + "<script>".length, close);
new vm.Script(source, { filename: "ontology-studio-inline.js" });
const mappingGraph = fs.readFileSync(new URL("./mapping-graph.js", import.meta.url), "utf8");
new vm.Script(mappingGraph, { filename: "ontology-mapping-graph.js" });

const ids = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map(x => x[1]));
const refs = new Set([...source.matchAll(/\$\(["']([^"']+)["']\)/g)].map(x => x[1]));
const missing = [...refs].filter(x => !ids.has(x));
if (missing.length) throw new Error("Missing DOM ids: " + missing.join(", "));
for (const endpoint of ["/kg/sessions", "/automodel/jobs", "/ontology/metrics", "/studio/schema-graph", "/studio/instance-graph", "/studio/quality", "/studio/governance", "/studio/provenance", "/studio/provenance/source", "/automodel/versions/diff", "/pipeline"]) {
  if (!html.includes(endpoint)) throw new Error("Missing expected API integration: " + endpoint);
}
for (const id of ["schemaMode", "instanceMode", "graphZoomIn", "graphZoomOut", "graphFit", "traceEntity", "traceButton", "sourceTable", "sourceColumn", "reverseTraceButton", "versionA", "versionB", "compareVersions", "governanceSummary", "lineageDetails", "semanticMappingSvg", "semanticMappingDetail", "semanticMappingSummary"]) {
  if (!ids.has(id)) throw new Error("Missing expected workbench control: " + id);
}
console.log("Ontology Studio V2 static validation passed");
