# Ontology Modeling Standards

## Purpose

This deterministic standards checker complements structural validation, data-mapping validation, CQ assessment and human review. It evaluates whether a model is understandable and maintainable as a business ontology rather than merely being syntactically valid.

## Rule groups

1. **Business object definition**: PascalCase type names, business label and description, valid object kind, stable identity, title/display property and inheritance integrity.
2. **Property definition**: snake_case names, descriptions, supported value ranges, controlled enum values, temporal roles, identifier semantics and valid sensitivity classification.
3. **Source binding**: source and dataset/table identification, stable source primary key, non-empty mappings and valid target properties.
4. **Business relationships**: snake_case relation names, business label/definition, explicit endpoint types, valid cardinality, valid endpoint references, inverse relationship references, temporal bounds and many-to-many association-source guidance.
5. **Reusable interfaces**: each object claiming an interface must implement that interface's declared property contract.

## Severity policy

- **error**: deterministic semantic or structural defect; blocks the pipeline release path.
- **warning**: incomplete business documentation or modeling best practice; surfaced for reviewer attention and does not independently block publication.
- **info**: reserved for non-blocking advisory findings.

The checker returns status, severity counts, findings with stable rule codes and remediation recommendations, and the policy used. It is deterministic and does not ask an LLM to judge its own output.

## Pipeline integration

Automodel pipeline execution and post-review rebuild both run source mapping contract validation, business-object semantic validation and ontology modeling standards validation.

All findings are exposed in job.standards_report; errors and warnings are also included in job.issues with recommendations. Errors block materialization/release. Warnings remain visible and cause the run to be marked for review where applicable.

## Initial rule catalog

- Object: OBJECT_NAME_NOT_PASCAL_CASE, OBJECT_LABEL_MISSING, OBJECT_DESCRIPTION_MISSING, OBJECT_IDENTITY_MISSING, OBJECT_TITLE_MISSING, OBJECT_PARENT_UNKNOWN, OBJECT_INHERITANCE_CYCLE.
- Property: PROPERTY_NAME_NOT_SNAKE_CASE, PROPERTY_DESCRIPTION_MISSING, PROPERTY_RANGE_INVALID, ENUM_VALUES_MISSING, TEMPORAL_ROLE_MISSING, PROPERTY_SENSITIVITY_INVALID.
- Mapping: SOURCE_BINDING_INCOMPLETE, SOURCE_PRIMARY_KEY_MISSING, SOURCE_TARGET_PROPERTY_UNKNOWN, SOURCE_MAPPING_EMPTY.
- Relationship: RELATION_NAME_NOT_SNAKE_CASE, RELATION_ENDPOINT_UNTYPED, RELATION_ENDPOINT_UNKNOWN, RELATION_CARDINALITY_INVALID, RELATION_INVERSE_UNKNOWN, TEMPORAL_RELATION_BOUNDS_MISSING, MANY_TO_MANY_WITHOUT_ASSOCIATION_SOURCE.
- Interface: INTERFACE_UNKNOWN, INTERFACE_PROPERTY_NOT_IMPLEMENTED.

The catalog is versioned in code by stable rule codes. Thresholds and domain-specific extensions can be added without changing the report contract.