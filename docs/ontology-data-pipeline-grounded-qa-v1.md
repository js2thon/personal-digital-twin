# Ontology Data Pipeline & Grounded QA Hardening (V1)

## Goal

Make ontology-backed answers accurate, concise, and auditable by first improving source ingestion and mapping quality. A query must not silently treat discovery samples as complete production data.

## Delivery order

### Phase A — Data source and mapping

1. Separate source discovery from production ingestion. Discovery samples may profile and propose ontology mappings, but may never be treated as production facts.
2. Standardize source adapters around a read-only metadata/profile interface and a streaming row interface. Keep credentials outside job payloads and logs.
3. Require stable source record keys for upsert. Append mode is immutable and idempotent; upsert mode must reject missing or duplicate source keys.
4. Persist mapping contracts with source identity, source schema fingerprint, ontology version, mapping version, type conversions, null policy, key strategy, and relation strategy.
5. Validate mappings at three levels:
   - structural: source column and ontology target exist; no ambiguous duplicate targets;
   - semantic: datatype compatibility, required fields, key uniqueness, relationship cardinality and referential integrity;
   - data: conversion failures, null ratios, value-domain violations, duplicate keys and dangling references.
6. Keep every field's raw value, transformed value, source/table/row/column, mapping version and ontology version in lineage.
7. Reconcile full snapshots only after all batches succeed. Failed or partial runs must not deactivate records. Incremental change feeds must use source watermarks or CDC offsets, not discovery sample row counts.
8. Publish a mapping only after validation and human review. Execution must only accept an approved immutable mapping version.

### Phase B — Ontology-grounded QA

1. Resolve user language to registered ontology classes, properties, relations and metrics.
2. Generate a constrained query plan (AST), validate every class/property/dimension/filter/time condition, then execute deterministic retrieval or metric computation.
3. Do not let the LLM generate executable SQL/SPARQL/Cypher. The LLM may propose a plan and summarize verified results only.
4. Attach provenance to the answer: ontology version, mapping version, data scope, source/table/column, matched instance IDs, metric definition and computation result.
5. For exact row-level traceability, return field-lineage records from the ingestion store. A schema mapping alone is not sufficient to claim row-level provenance.
6. Refuse or clarify when the question is ambiguous, a target cannot be uniquely resolved, evidence is missing/conflicting, or the requested metric is not registered.
7. Keep answers concise: one-sentence conclusion by default, up to three short evidence items; no chain-of-thought.
8. Explicitly label Discovery-sample answers. Never present sample counts as source-system totals.

## Acceptance criteria

### Data and mapping

- Replaying the same immutable snapshot and mapping version produces no duplicate instances.
- Upsert rejects missing/duplicate source IDs and updates only changed records.
- Failed batches do not advance source watermarks or trigger missing-record reconciliation.
- Every mapped property can be traced to a source table, source row, source column, raw value and transformed value after production ingestion.
- Mapping validation reports structural, semantic and data-quality errors separately.
- A mapping with unresolved required fields, invalid type conversions, unstable keys or dangling required relations cannot be approved/published.

### QA

- Every returned fact/metric includes an evidence envelope and explicit data scope.
- Every instance-level answer can be resolved to source field lineage; every aggregate answer includes metric definition and the mapped source fields used.
- Ambiguous entity matches do not return a guessed answer.
- Invalid time filters, unsupported dimensions, unknown properties and unregistered metrics fail closed.
- No-evidence questions return a concise “cannot confirm” response with the missing evidence identified.
- LLM output cannot change verified values and is bounded to concise Chinese.
- Automated tests cover positive, negative, ambiguous, empty-data, cross-tenant and lineage cases.

## Current increment in this branch

- Ontology QA now attaches source mapping metadata and explicitly marks Automodel results as `discovery_sample`, preventing a sample answer from being mistaken for a full-source statistic.
- QA time filters reject non-temporal attributes, wrong object ownership and malformed/reversed ranges.
- Grounded LLM answers are constrained to concise Chinese, disallow invented facts and explicitly fail closed on insufficient or conflicting evidence.
- Tests cover provenance metadata and time-filter validation.

## Known remaining work

- The current source evidence is mapping-level (source/table/column) plus returned instance IDs. It is not yet a substitute for durable row-level field-lineage lookup.
- The production mapping executor currently consumes tabular rows; database discovery and production database streaming ingestion still need a first-class connector adapter with keyset pagination/watermarks.
- Metric-level provenance needs dependency expansion down to every contributing property and source row, not only the metric's declared frame.
- Graph/RDF projection consistency, incremental change handling and production QA over the unified semantic store need end-to-end acceptance tests.
