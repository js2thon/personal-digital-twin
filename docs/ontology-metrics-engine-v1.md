# Ontology Metrics Engine V1

## Scope

The metrics layer is a governed semantic asset layer, separate from chart rendering.
The existing ontology runtime already compiles ontology modeling specs to IR and supports
native aggregation functions and row-level derived expressions. This V1 adds a dedicated
metric-definition lifecycle and a dependency-aware derived/composite metric layer.

## Metric kinds

- **native**: count, distinct_count, sum, avg, min, max over a declared ontology subject/field.
- **derived**: arithmetic formula over metric IDs, e.g. `active_count / employee_count * 100`.
- **composite**: weighted/combined arithmetic formula over approved metric IDs. Weighting and
  normalization remain business-owned; the platform validates syntax, dependency and units,
  but never invents weights.

## Formula safety and validation

Formulas are parsed with Python AST and evaluated by a strict node whitelist. Calls, attribute
access, indexing, comprehensions, imports, and arbitrary code are rejected. Division/modulo by
zero and non-finite results return an unavailable result, not a fabricated zero. The dependency
graph is topologically sorted; missing dependencies and cycles block validation. Addition and
subtraction require matching declared units.

## API

All endpoints are under `/api/v1` and inherit the global identity/tenant middleware:

- `GET /ontology/metrics/catalog`
- `POST /ontology/metrics/discover` body: `{ "spec": { ... } }`
- `POST /ontology/metrics/validate` body: `{ "items": [ ... ] }`
- `POST /ontology/metrics/preview` body: `{ "items": [ ... ], "data": { "entities": { ... } } }`
- `POST /ontology/metrics/preview-from-store` body: `{ "metric_ids": [ ... ], "limit": 5000 }` (reads tenant-scoped semantic instances)
- `GET /ontology/metrics?metric_id=...`
- `POST /ontology/metrics` body: `{ "definition": { ... }, "ontology_version": "..." }`
- `POST /ontology/metrics/{metric_id}/approve`
- `POST /ontology/metrics/{metric_id}/publish`

Metric definitions are tenant-scoped, versioned and checksum-addressed. Save produces
`validated`; a separate explicit approval moves to `approved`; only the latest approved
version can be published. Formula/dependency definitions are immutable snapshots per version.

## Definition example

```json
{
  "metric_id": "active_rate",
  "name": "在职率",
  "kind": "derived",
  "formula": "active_count / employee_count * 100",
  "dependencies": ["active_count", "employee_count"],
  "unit": "%"
}
```

## Boundaries

Discovery is a candidate generator, not a semantic authority. The business owner must confirm
entity grain, distinct key, filters, null policy, units, time grain, and denominator definitions.
The manual V1 preview consumes caller-provided ontology-shaped `data.entities`. The governed
`preview-from-store` endpoint additionally reads the existing tenant-scoped
`ontology.entity_instances` semantic store populated by the approved dataset ingestion pipeline.
It computes only when the full active instance set is within the requested limit and visible
to the caller; otherwise it returns an explicit incomplete result without calculating. It rejects
mixed ontology versions and metric/data ontology-version mismatches. It does not read or silently
bridge RDF KG Session data.

SQL/SPARQL compilation for arbitrary external sources, cross-source joins, dimension slicing,
time-window comparison, dashboard binding and full composite normalization are later phases.
No metric is auto-published by an LLM.
