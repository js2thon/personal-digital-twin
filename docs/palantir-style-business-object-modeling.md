# Palantir-style Business Object Ontology Modeling

## Goal

Evolve ontology modeling from table-to-class inference into a business-object-centered model. The implementation is additive and preserves the existing LinkML/CQ schema and API compatibility.

## Modeling contract

### Business object types
A business object type represents a real-world entity, event, transaction, reference object, or association. It carries:
- Stable business identity rules (source system, one or more key properties, normalization and priority).
- A title/display property for canonical object views.
- One or more backing source bindings, each with a source, dataset/table, primary key, property mapping, and materialized/federated/action-created mode.
- Optional parent type, interfaces, lifecycle property, aliases and constraints.

### Properties
Properties retain their existing range and required constraints, and can additionally describe:
- Business semantic type, unit, temporal role and source mappings.
- Sensitivity classification (public, internal, confidential, restricted).
- Identifier/title roles.

The automatic modeler uses conservative name-based sensitivity hints only. They are not a substitute for data classification and must be reviewed before production use.

### Business links
Links retain source/target type and cardinality, with additional fields for inverse relation, relationship kind, temporal validity, backing source bindings and link properties.

### Interfaces
Interfaces describe reusable property and capability contracts for unrelated object types. An object type may implement multiple interfaces.

## Implemented in this increment

- Extend the semantic Schema-as-Code Pydantic contract with ObjectIdentityRule, ObjectSourceBinding, InterfaceDef, enriched AttributeDef, EntityTypeDef, and RelationTypeDef.
- Extend Automodel output with object kind, title property, source-specific identity rules, backing-source mapping, and object-owned property metadata.
- Pass source identity from the ingestion TableSet into model generation.
- Infer conservative property sensitivity hints and temporal roles from source field names.
- Add pure validation for identity keys, source binding keys/modes, property mappings, interface references, relation endpoints, and inverse relation consistency.
- Run business-model validation in both initial pipeline execution and post-review rebuild; validation errors block materialization/publication.
- Add regression coverage and include it in ontology CI.

## Not yet implemented

1. Cross-source entity resolution: matching and merging the same real-world object across HR/CRM/ERP systems.
2. Composite and alternate business-key resolution at runtime, including conflict queues and merge/split audit.
3. Multiple source tables merged into one generated object type. The current auto-generator still generates a class per non-junction table; the schema contract can describe multiple bindings, but automatic consolidation is a later increment.
4. Full interface inheritance/contract enforcement in object read/write APIs.
5. First-class Action and Function registries, typed parameters, preconditions, side effects, transaction/writeback, permissions and rollback.
6. Temporal relationship runtime, bitemporal property history and as-of object queries.
7. Object Sets, canonical Object Views, runtime security enforcement and dependency impact analysis.

## Acceptance principles

- A generated object must have a stable identity or an explicit action-created source.
- A source binding must define a stable source primary key unless it is action-created.
- Every mapped property must exist on the target object.
- Every link endpoint must resolve to a registered object type.
- Inverse links must be reciprocal.
- High-sensitivity properties must not be exposed merely because the modeler inferred them; production authorization remains a separate policy decision.
- The ontology is only publishable when data mapping validation and business-object validation both pass, with human review for low-confidence candidates.