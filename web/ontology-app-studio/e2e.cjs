const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const { spawn } = require("node:child_process");
const assert = require("node:assert/strict");

(async () => {

const server = spawn("python", ["-m", "http.server", "4173", "--bind", "127.0.0.1", "--directory", "web"], { stdio: "ignore" });
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
let browser;
try {
  let ready = false;
  for (let i = 0; i < 40; i++) {
    try { const response = await fetch("http://127.0.0.1:4173/ontology-app-studio/"); if (response.ok) { ready = true; break; } } catch {}
    await wait(250);
  }
  assert.ok(ready, "static workbench server did not start");
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const requests = [];
  const jobId = "job-e2e-001";
  const jobPath = "/automodel/jobs/" + jobId;
  const fixtures = {
    job: { id: jobId, source: { name: "E2E 客户数据" }, status: "draft", schema_version: "v1", draft: { schema: { schema_version: "v1" } } },
    overview: {
      object_types: [{ name: "Customer", label: "客户", count: 2, primary_key: "customer_id", source_table: "customers", properties: ["customer_name"] }],
      link_types: [{ name: "places", label: "下单", source: "Customer", target: "Order", cardinality: "one_to_many", count: 1 }],
      nodes: 2, edges: 1
    },
    schema: { nodes: [{ id: "CLASS:Customer", label: "客户", kind: "class" }, { id: "CLASS:Order", label: "订单", kind: "class" }], edges: [{ from: "CLASS:Customer", to: "CLASS:Order", label: "下单" }] },
    quality: { quality_score: 96, score_label: "excellent", node_count: 2, edge_count: 1, coverage: { mapping_ratio: 1 }, review_pending: [], findings: [] },
    governance: { asset: { status: "draft" }, release_gate: { ready: false, reasons: ["待审核"] }, review: { pending: 0 }, access: { authorization_enforced_by: "backend" } },
    pipeline: { status: "draft", nodes: [{ id: "source", label: "数据源", state: "done", detail: "1 张表" }] },
    instanceGraph: { nodes: [{ id: "customer-1", label: "张三", group: "Customer" }, { id: "order-1", label: "订单 001", group: "Order" }], edges: [{ from: "customer-1", to: "order-1", label: "下单" }] },
    objects: { total: 1, columns: [{ key: "customer_name", label: "客户名称" }], rows: [{ _id: "customer-1", _name: "张三", customer_name: "张三" }] },
    registry: { items: [{ metric_id: "customer_count", name: "客户总数", version: 1, status: "published", ontology_version: "v1", definition: { kind: "native" } }] },
    jobMetrics: { metrics: { customer_count: { name: "客户总数", kind: "native" } }, stats: { total: 1 }, issues: [], lineage: { nodes: [{ id: "customer_count" }], edges: [] } }
  };

  await page.route("**/api/v1/**", async route => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace("/api/v1", "");
    requests.push({ path, method: route.request().method() });
    let body;
    if (path === "/automodel/jobs") body = { items: [{ id: jobId, source: fixtures.job.source, status: "draft", created_at: "2026-11-26" }] };
    else if (path === jobPath) body = fixtures.job;
    else if (path === jobPath + "/studio/overview") body = fixtures.overview;
    else if (path === jobPath + "/studio/schema-graph") body = fixtures.schema;
    else if (path === jobPath + "/studio/quality") body = fixtures.quality;
    else if (path === jobPath + "/studio/governance") body = fixtures.governance;
    else if (path === jobPath + "/pipeline") body = fixtures.pipeline;
    else if (path === jobPath + "/studio/instance-graph") body = fixtures.instanceGraph;
    else if (path.startsWith(jobPath + "/studio/objects")) body = fixtures.objects;
    else if (path === "/ontology/metrics") body = fixtures.registry;
    else if (path === jobPath + "/metrics") body = fixtures.jobMetrics;
    else {
      await route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ detail: "Unmocked API route: " + path }) });
      return;
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });

  await page.goto("http://127.0.0.1:4173/ontology-app-studio/", { waitUntil: "networkidle" });
  await page.waitForFunction(() => document.querySelector("#connection")?.textContent.includes("已连接后端"));
  assert.match(await page.locator("#objectNav").innerText(), /客户/);
  assert.match(await page.locator("#assetStatus").innerText(), /job-e2e-001/);

  await page.locator('[data-view="instances"]').click();
  await page.locator("#classSelect").selectOption("Customer");
  await page.locator("#searchInstances").click();
  await page.waitForFunction(() => document.querySelector("#instanceTable")?.innerText.includes("张三"));
  assert.match(await page.locator("#instanceTable").innerText(), /客户名称/);

  await page.locator('[data-view="metrics"]').click();
  await page.locator("#loadMetrics").click();
  await page.waitForFunction(() => document.querySelector("#metricTable")?.innerText.includes("客户总数"));
  assert.match(await page.locator("#metricSummary").innerText(), /当前任务 1 项/);

  const expected = [
    "/automodel/jobs", jobPath + "/studio/overview", jobPath + "/studio/schema-graph",
    jobPath + "/studio/quality", jobPath + "/studio/governance", jobPath + "/pipeline",
    jobPath + "/studio/instance-graph", jobPath + "/studio/objects", "/ontology/metrics", jobPath + "/metrics"
  ];
  for (const path of expected) assert.ok(requests.some(r => r.path === path || (path === jobPath + "/studio/objects" && r.path.startsWith(path))), "expected API request missing: " + path);
  console.log("Ontology App Studio browser/API contract E2E passed.");
 } finally {
  if (browser) await browser.close();
  server.kill();
}
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
