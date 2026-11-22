# Ontology Pipeline Hardening — V1

## Scope

This change consolidates the first enterprise-hardening pass for the Automodel / Ontology Studio pipeline. It is intentionally additive and does not merge itself into main.

## Implemented

### 1. Tenant isolation
- The in-process Automodel job cache now stores tenant ownership alongside each cached job.
- A cache hit is returned only when the requested tenant matches the cached owner; cross-tenant cache hits return not-found.
- In-memory listing is tenant-filtered.
- Database upserts cannot mutate an existing job owned by another tenant; the conflict update is guarded by tenant equality.

### 2. One release gate
- Added `automodel.release_gate` as the shared deterministic release decision.
- The gate checks ontology structure, mapping contract presence/errors, human-review backlog, preview generation, materialization errors, dangling references and optional CQ results.
- `pipeline.approve_and_publish` enforces the gate immediately before publishing.
- `studio.governance` exposes the same gate result; the UI summary is no longer an independent approximation.
- CQ can be made mandatory using `job.config.require_cq_for_publish=true`; when a CQ result exists, failures or accuracy below 95% block release.

### 3. Review and change audit
- Review actions are validated; unsupported object types, unknown references and unsupported actions fail explicitly rather than being silently ignored.
- `accept`, `reject`, `rename` and relation `retarget` are supported.
- Each decision records a change ID, actor, UTC review time, schema version, target, before/after values, evidence and comment in `change_set` and `actions_log`.
- Rename and retarget are followed by the normal rebuild/contract-validation step before publication.

### 4. Mapping contract validation
- Contract validation now optionally consumes source profiles.
- It checks source table/column existence, primary-key presence, sampled nullability/uniqueness, relation endpoints, FK columns, relation target mapping and junction-table columns.
- Pipeline generation and rebuild both pass source profiles into validation.
- Sample-level uniqueness is evidence, not a substitute for a database uniqueness constraint or full-source validation.

### 5. Bounded, resumable ingestion orchestration
- Added `ontology.ingestion_batches.execute_approved_dataset_stream`.
- It executes an iterable of trusted row dictionaries in bounded batches (maximum 500 rows), using the existing approved mapping executor for each atomic batch.
- A snapshot ID and deterministic batch digest prevent silently reusing a snapshot ID for changed content.
- Successful batches are checkpointed and skipped on replay; a batch whose execution committed but whose checkpoint update failed is safe to replay because the underlying mapping executor is idempotent.
- Upsert mode refreshes source-record presence. Optional reconciliation marks source records absent from a completed snapshot as inactive.

## Explicit operational boundaries

- The new batch orchestrator accepts a trusted iterable of rows. It does **not** itself create a production database connector, store database credentials, or perform SQL keyset pagination/watermark extraction. The existing Automodel database connector remains a discovery/profiling sampler.
- Reconciliation is a **soft tombstone ledger** only: it sets `dataset_source_records.source_active=false`. It does not physically delete ontology instances or facts. A downstream retention/deletion policy and audited delete executor are still required.
- Checkpoints are per batch. A caller must replay the same immutable source snapshot with the same snapshot ID to resume. The connector that produces the iterable must guarantee snapshot consistency.
- Each batch is atomic, but the entire multi-batch snapshot is not one global transaction. A failed later batch leaves earlier successful batches committed and resumable.
- CQ enforcement is optional unless the job is configured to require it. CQ generation/execution is still a separate evaluation step.
- Full browser E2E, real multi-tenant PostgreSQL security tests, external database keyset pagination, deletion execution, and operational load tests remain deployment-level acceptance work.

## Required rollout

1. Deploy the code.
2. Run the ontology mapping/ingestion migrations and ensure the mapping executor schema is initialized before using batch checkpoints.
3. Ensure application DB role can create/alter the new checkpoint table and add `source_active` to `dataset_source_records`, or pre-apply equivalent migration DDL.
4. Set `require_cq_for_publish=true` for environments where CQ is a mandatory release criterion.
5. Run the regression suite and then validate tenant isolation and snapshot replay against a disposable PostgreSQL database.
6. Keep physical deletion disabled until the enterprise retention policy and audited delete executor are approved.

## Regression coverage added

- Tenant-scoped in-memory cache lookup and listing.
- Release-gate blockers for contract errors, pending reviews, dangling references and CQ failures.
- Review action validation and immutable audit metadata.
- Deterministic batch boundaries and digest stability.

