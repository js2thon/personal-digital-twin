-- 05: 清理已下线的旧「本体建模」RDF 导入历史数据。
-- 仅按旧导入模块写入的显式来源标记删除；新 Pipeline 数据不带该标记，不受影响。
-- facts -> relations -> orphan entities，避免违反外键并保留仍被新业务引用的实体。
-- source_ref 在历史数据库升级时由 ontology.ensure_schema 补列；此处先幂等补列，
-- 确保全新数据库在执行初始化脚本时也能安全执行本清理。

ALTER TABLE knowledge.facts
    ADD COLUMN IF NOT EXISTS source_ref JSONB DEFAULT '{}'::jsonb;

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
