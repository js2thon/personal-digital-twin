"""可直接运行的示例工作流（含模拟数据）。

演示「建模即流程」完整闭环：画布 7 类建模设计节点 → 拓扑执行 → 四类完整验证 → 发布门禁。
模拟数据仅用于验证机制（mode=simulated），明确标注，绝不冒充真实业务数据。

字段→属性映射内化到实体/属性/类型节点（source_field / is_pk / target_entity / source_fields）；
实例化写入 / 完整验证 / 发布版本在收尾区，发布门禁按四类验证报告判定。

实体样例沿用项目真实 Employee/Project 语境（Employee.full_name 必填），
但数据是本地构造的模拟值，不来自真实库。
"""
from __future__ import annotations

from typing import Any


def sample_spec() -> dict[str, Any]:
    """7 类建模设计节点齐全的示例工作流，节点按画布执行顺序排列并建立连线。"""
    nodes = [
        # 1 数据源（多源并行示例：文件 + PostgreSQL）
        {"id": "ds_file", "kind": "datasource", "x": 40, "y": 80, "fields": {
            "name": "员工花名册", "source_type": "file", "file_name": "employee_roster.csv",
            "file_format": "csv", "source": "HR 系统导出", "cadence": "每日 08:00"}},
        {"id": "ds_pg", "kind": "datasource", "x": 40, "y": 200, "fields": {
            "name": "项目主数据", "source_type": "postgres", "db_host": "127.0.0.1",
            "db_port": "5432", "db_name": "hr_prod", "db_table": "project",
            "db_user": "pdt_reader", "cadence": "实时"}},
        # 2 探查清洗
        {"id": "prof_1", "kind": "profiler", "x": 240, "y": 120, "fields": {
            "name": "花名册清洗", "input": "员工花名册", "profile_rules": "null,unique,type",
            "clean_rules": "trim(employee_no,full_name,email)\n"
                           "dedup(employee_no, keep=first)\n"
                           "drop_null(employee_no,full_name)", "output": "clean_employee"}},
        # 3 业务对象 / 实体定义（Schema 映射内化到本节点：source_fields / target_entity）
        # fact_entity_type + aliases：本体里声明该对象在事实库里的物理类型（问数推导根）
        {"id": "ent_emp", "kind": "entity", "x": 440, "y": 60, "fields": {
            "name": "Employee", "label": "员工", "primary_key": "employee_no",
            "fact_entity_type": "Person", "aliases": "员工,人员,人",
            "attributes": "employee_no:string:required\nfull_name:string:required\ndepartment:string\nlevel:string\nemail:string:required\nstatus:string\nskills_vec:string\nrisk_level:string",
            "type_system": "parent=  # 根类型", "source_object": "ontology.entity_instances:Employee",
            # 内化的字段→属性映射（源字段=属性）
            "target_entity": "Employee",
            "source_fields": "emp_no,full_name,dept,level,email",
            "mappings": "emp_no=employee_no\nfull_name=full_name\ndept=department\nlevel=level\nemail=email"}},
        {"id": "ent_proj", "kind": "entity", "x": 440, "y": 260, "fields": {
            "name": "Project", "label": "项目", "primary_key": "project_id",
            "attributes": "project_id:string:required\nproject_name:string:required\napproval_status:string:required:pending,approved,archived",
            "type_system": "parent=", "source_object": "ontology.entity_instances:Project",
            "target_entity": "Project", "source_fields": "proj_id,proj_name",
            "mappings": "proj_id=project_id\nproj_name=project_name"}},
        # 3b 语义属性节点：把「在职」语义映射到事实库物理字段（问数可推导的根）
        #    这些字段是本体建模产物，用户可在画布属性详情里编辑，不是问数代码硬编码
        {"id": "attr_status", "kind": "entity", "x": 640, "y": 430, "fields": {
            "name": "status", "label": "在职状态", "range": "string",
            "value_meaning": "在职", "fact_predicate": "status", "fact_value": "A",
            "desc": "语义映射：value_meaning=在职、fact_predicate=status、fact_value=A；"
                    "据真实库 status 唯一取值 A、active 27 人与 Employee 实例吻合，"
                    "为可替换口径假设（本体正式定义后改此节点即可）"}},
        # 检索类语义属性：无 fact_value → 返回「属性值」而非过滤计数。
        # 对应真实快照 数据管理部人员.json 的 dept_manager_name 列（25 条真实事实已灌入知识层）。
        {"id": "attr_dept_manager", "kind": "entity", "x": 640, "y": 520, "fields": {
            "name": "dept_manager_name", "label": "部门负责人", "range": "string",
            "value_meaning": "部门负责人", "fact_predicate": "dept_manager_name",
            "desc": "检索类语义映射：value_meaning=部门负责人、fact_predicate=dept_manager_name、"
                    "无 fact_value（问「部门负责人是谁」时返回属性值，不做计数）。值来自真实快照；"
                    "多对一（多个员工共享一个负责人），答案按负责人去重并给管辖人数"}},
        # 4 关系定义
        {"id": "rel_member", "kind": "relation", "x": 640, "y": 80, "fields": {
            "name": "member_of", "source_entity": "Employee", "target_entity": "Project",
            "cardinality": "N:M", "directed": True, "edge_properties": "role,since"}},
        # 5 约束 & 规则
        {"id": "con_email", "kind": "constraint", "x": 640, "y": 200, "fields": {
            "name": "邮箱唯一", "kind": "unique", "target": "Employee.email",
            "expression": "unique(Employee.email)", "severity": "error"}},
        {"id": "con_level", "kind": "constraint", "x": 640, "y": 320, "fields": {
            "name": "职级范围", "kind": "business", "target": "Employee.level",
            "expression": "level >= 1", "severity": "warn"}},
        # 6 Function
        {"id": "fn_risk", "kind": "function", "x": 840, "y": 80, "fields": {
            "name": "fn_derive_risk", "kind": "derive", "inputs": "level,department",
            "output": "risk_level", "logic": "职级低且项目紧时 risk_level=high",
            "stateful": False}},
        # 7 Action / Monitor
        {"id": "act_notify", "kind": "action", "x": 840, "y": 220, "fields": {
            "name": "act_notify_on_approve", "role": "action", "trigger": "event",
            "condition": "approval_status 变为 approved", "effect": "回调通知并写回本体对象",
            "cadence": "事件即时", "closed_loop": True}},
    ]
    edges = [
        {"from": "ds_file", "to": "prof_1"}, {"from": "ds_pg", "to": "ent_proj"},
        {"from": "prof_1", "to": "ent_emp"},
        {"from": "ent_emp", "to": "attr_status"},  # 属性归属：status 挂在 Employee 上
        {"from": "ent_emp", "to": "attr_dept_manager"},  # 部门负责人挂在 Employee 上
        {"from": "ent_emp", "to": "rel_member"}, {"from": "ent_proj", "to": "rel_member"},
        {"from": "ent_emp", "to": "con_email"}, {"from": "ent_emp", "to": "con_level"},
        {"from": "ent_emp", "to": "fn_risk"}, {"from": "ent_proj", "to": "act_notify"},
    ]
    # 收尾区配置：实例化 + 发布（不属于画布节点，随 spec 传入后端）
    return {"version": "1.0", "note": "示例工作流（7 类建模节点 + 模拟数据；收尾区实例化/发布按报告判定）",
            "nodes": nodes, "edges": edges,
            "instantiate": {"target_entity": "Employee", "write_mode": "upsert",
                            "idempotency_key": "employee_no", "source_ref": "花名册清洗"},
            "publish": {"version": "v1.0.0", "channel": "local", "gate_validation": True}}


def sample_data() -> dict[str, Any]:
    """模拟数据：5 名员工 / 2 个项目 / 5 条成员关系；必填全填、引用完整、无孤立。"""
    employees = [
        {"id": "E001", "properties": {"employee_no": "E001", "full_name": "张伟", "department": "研发", "level": "3", "email": "zhang@ex.com", "skills_vec": [0.1, 0.2, 0.3]}},
        {"id": "E002", "properties": {"employee_no": "E002", "full_name": "李娜", "department": "产品", "level": "2", "email": "lina@ex.com", "skills_vec": [0.4, 0.5]}},
        {"id": "E003", "properties": {"employee_no": "E003", "full_name": "王强", "department": "研发", "level": "1", "email": "wangq@ex.com", "skills_vec": [0.7, 0.8, 0.9]}},
        {"id": "E004", "properties": {"employee_no": "E004", "full_name": "赵敏", "department": "测试", "level": "2", "email": "zhaom@ex.com", "skills_vec": [0.2, 0.4]}},
        {"id": "E005", "properties": {"employee_no": "E005", "full_name": "陈静", "department": "研发", "level": "4", "email": "chenj@ex.com", "skills_vec": [0.6, 0.1, 0.9]}},
    ]
    projects = [
        {"id": "P001", "properties": {"project_id": "P001", "project_name": "数字孪生平台", "approval_status": "approved"}},
        {"id": "P002", "properties": {"project_id": "P002", "project_name": "知识本体建设", "approval_status": "pending"}},
    ]
    relations = [
        {"source_id": "E001", "relation_type": "member_of", "target_id": "P001"},
        {"source_id": "E002", "relation_type": "member_of", "target_id": "P002"},
        {"source_id": "E003", "relation_type": "member_of", "target_id": "P001"},
        {"source_id": "E004", "relation_type": "member_of", "target_id": "P001"},
        {"source_id": "E005", "relation_type": "member_of", "target_id": "P002"},
    ]
    return {"mode": "simulated", "note": "本地模拟数据，仅用于验证机制，不代表真实业务数据",
            "entities": {"Employee": employees, "Project": projects}, "relations": relations}


def run_sample() -> dict[str, Any]:
    """一键跑通示例：返回完整验证报告 + 发布门禁结论。"""
    from . import workflow as WF

    return WF.run(sample_spec(), sample_data())
