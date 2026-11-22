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

## Pipeline P0/P1 增量整改

- Business Object Discovery 成为独立阶段，位于数据画像之后、本体草案生成之前。输出对象候选、身份候选、业务证据和跨表归并建议。
- 跨表归并默认不自动执行；审核人通过 `kind=class, action=merge, target=<object>` 明确确认后，才将多个源表归入同一业务对象。
- 审核重命名、删除和归并会同步更新 Schema 中的属性归属、对象关系端点、身份规则、展示属性、数据源绑定、Interface 引用及 class_map。
- 规范 Warning 允许继续预览和模拟，但必须由审核人显式确认；本体错误、映射错误和未解决审核事项阻断发布。
- 映射契约按对象的 `backing_sources` 与当前 `source_id`/table 匹配，支持不同来源分别映射到同一业务对象。
- Registry 发布按字段合并对象身份、接口、属性和多源绑定；语义冲突显式返回，不再以整对象覆盖方式静默丢弃候选定义。
- 发布过程记录 Schema 提交、运行时目录安装和指标注册状态；发生部分提交时任务进入 `publish_partial_failure`，不会伪报 `published`。

### Business Object Discovery 输出

```json
{
  "candidate_count": 2,
  "merge_candidate_count": 1,
  "candidates": [],
  "merge_candidates": [],
  "policy": {"auto_merge": false}
}
```

归并建议只是一组可审查证据（共享字段、身份候选、源表和置信度），不等同于实体解析或自动主数据合并。跨系统实体匹配、冲突值治理和 merge/split 工作流仍需后续建设。
