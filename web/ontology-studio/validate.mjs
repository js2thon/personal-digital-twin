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
for (const endpoint of ["/kg/sessions", "/automodel/jobs", "/ontology/metrics", "/studio/schema-graph", "/studio/instance-graph", "/studio/quality", "/studio/governance", "/studio/provenance", "/studio/provenance/source", "/automodel/versions/diff", "/automodel/jobs/", "/mapping-contract", "/mapping-contract/preview", "/pipeline"]) {
  if (!html.includes(endpoint)) throw new Error("Missing expected API integration: " + endpoint);
}
for (const id of ["schemaMode", "instanceMode", "graphZoomIn", "graphZoomOut", "graphFit", "traceEntity", "traceButton", "sourceTable", "sourceColumn", "reverseTraceButton", "versionA", "versionB", "compareVersions", "governanceSummary", "lineageDetails", "semanticMappingSvg", "semanticMappingDetail", "semanticMappingSummary", "mappingValidate", "mappingPreview", "mappingPreviewStatus", "mappingPreviewSummary", "mappingPreviewEntities", "mappingPreviewFacts", "mappingPreviewIssues", "mappingSave", "mappingPublish"]) {
  if (!ids.has(id)) throw new Error("Missing expected workbench control: " + id);
}

if (mappingGraph.includes("inspect(left.find") || mappingGraph.includes("right.find(x=>x.kind")) {
  throw new Error("Mapping graph still references removed left/right layout variables");
}
if (!mappingGraph.includes("const first=[...sourceFields.values()]") || !mappingGraph.includes("schema.entity_types||{}")) {
  throw new Error("Mapping graph must initialize from current columns and include schema-only object types");
}

// Execute the graph renderer against a minimal DOM to catch runtime regressions
// that syntax-only validation cannot detect (for example stale layout variables).
class MockElement {
  constructor(tag = "div") { this.tagName = tag; this.children = []; this.attrs = {}; this.listeners = {}; this.innerHTML = ""; this.textContent = ""; this.value = ""; }
  replaceChildren(...items) { this.children = [...items]; }
  append(...items) { this.children.push(...items); }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  insertAdjacentHTML(_where, html) { this.innerHTML += html; }
}
const elements = new Map();
const documentMock = {
  getElementById(id) { if (!elements.has(id)) elements.set(id, new MockElement()); return elements.get(id); },
  createElementNS(_ns, tag) { return new MockElement(tag); }
};
const windowMock = {};
vm.runInNewContext(mappingGraph, { document: documentMock, window: windowMock, console });
const sampleJob = {
  contract: { source: "sample", mappings: [], links: [] },
  schema: {
    entity_types: { Alarm: { label: "报警事件" }, WorkOrder: { label: "检修工单" } },
    attributes: { alarmNo: { owner: "Alarm", label: "报警编号", range: "String" } },
    relation_types: { escalatesTo: { label: "升级派单", source: "Alarm", target: "WorkOrder", cardinality: "many_to_one" } }
  },
  profiles: {
    alarm: { primary_key: ["id"], columns: { id: { dtype: "Long" }, work_order_id: { dtype: "Long" } } },
    work_order: { primary_key: ["id"], columns: { id: { dtype: "Long" } } }
  }
};
windowMock.renderSemanticMappingGraph(sampleJob);
const graphMock = documentMock.getElementById("semanticMappingSvg");
if (!graphMock.children.length) throw new Error("Mapping graph did not render sample profile/schema nodes");
const relationNode = graphMock.children.flatMap(group => group.children || []).find(node => node.attrs?.["data-node"] === "relation:escalatesTo");
if (!relationNode) throw new Error("Schema-only relation node was not rendered");
relationNode.listeners.click?.();
if (!documentMock.getElementById("semanticMappingDetail").innerHTML.includes("编辑多对一关系映射")) {
  throw new Error("Clicking relation node did not open its configuration editor");
}

console.log("Ontology Studio V2 static and mapping-graph runtime validation passed");
