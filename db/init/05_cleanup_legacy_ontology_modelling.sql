-- 05: 清理已下线的旧「本体建模」RDF 导入历史数据。
-- 仅按旧导入模块写入的显式来源标记删除；新 Pipeline 数据不带该标记，不受影响。
-- facts -> relations -> orphan entities，避免违反外键并保留仍被新业务引用的实体。

DELETE FROM knowledge.facts
WHERE source_ref->>'kind' = 'rdf-import';

DELETE FROM ontology.relations
WHERE properties->>'source' = 'rdf-import';

DELETE FROM ontology.entities e
WHERE e.properties->>'source' = 'rdf-import'
  AND NOT EXISTS (
      SELECT 1 FROM knowledge.facts f WHERE f.subject_id = e.id
  )
  AND NOT EXISTS (
      SELECT 1 FROM ontology.relations r
      WHERE r.source_entity_id = e.id OR r.target_entity_id = e.id
  );
