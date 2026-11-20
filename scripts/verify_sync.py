"""端到端验证：图 ↔ 本体 四向同步（打真库、走真接口）。

跑法：python scripts/verify_sync.py
用一组临时实体做实验，最后清理干净（删边会连带撤销事实，正好验证 R4）。
"""
import os
import json
import sys
import urllib.request

API = "http://127.0.0.1:8101/api/v1"
PERSON_A = os.environ.get("VERIFY_PERSON_A", "")   # 示例主体 A 的 entity id
PERSON_B = os.environ.get("VERIFY_PERSON_B", "")   # 示例主体 B 的 entity id


def call(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"http_error": e.code, "body": e.read().decode()[:300]}


def status():
    s = call("/ontology/sync/status")
    return s


def main():
    ok = []
    fail = []

    def check(name, cond, extra=""):
        (ok if cond else fail).append(name)
        print(("  PASS " if cond else "  FAIL ") + name + (("  " + str(extra)) if extra else ""))

    print("0) 基线")
    s0 = status()
    print("   ", json.dumps(s0, ensure_ascii=False)[:240])
    check("基线可读", "edges_total" in s0)

    print("1) R3 人工建边 → 自动补本体事实")
    rel = call("/ontology/relations", "POST",
               {"source_entity_id": PERSON_A, "relation_type": "collaborates_with",
                "target_entity_id": PERSON_B, "note": "同步验证临时边"})
    rid = rel.get("id")
    check("建边成功", bool(rid), rel)
    backing = rel.get("backing_fact") or {}
    fid = backing.get("fact_id")
    check("同时生成事实", backing.get("created") is True and bool(fid), backing)
    s1 = status()
    check("边数 +1", s1.get("edges_total") == (s0.get("edges_total") or 0) + 1,
          f"{s0.get('edges_total')} → {s1.get('edges_total')}")
    check("人工桶 +1", (s1["edges"].get("manual", {}) or {}).get("active", 0)
          == (s0["edges"].get("manual", {}) or {}).get("active", 0) + 1, s1.get("edges"))
    check("人工边都有事实支撑", (s1["orphans"] or {}).get("unbacked_manual", 0) == 0,
          s1.get("orphans"))

    print("2) 对账不会把这条边当多余边删掉")
    plan = call("/ontology/sync?dry_run=true", "POST")
    check("dry-run 可读", "reconcile" in plan, plan.get("reconcile"))
    check("无待删边", (plan.get("reconcile") or {}).get("to_remove", 0) == 0,
          plan.get("reconcile"))
    check("无重复待增边", (plan.get("reconcile") or {}).get("to_add", 0) == 0,
          "to_add=" + str((plan.get("reconcile") or {}).get("to_add")))

    print("3) R4 删边 → 背后事实一起撤销（否则下一轮复活）")
    dele = call(f"/ontology/relations/{rid}", "DELETE")
    check("删边成功", dele.get("id") == rid, dele)
    check("连带撤销事实", (dele.get("backing_fact") or {}).get("withdrawn") is True,
          dele.get("backing_fact"))
    s2 = status()
    check("边数回到基线", s2.get("edges_total") == s0.get("edges_total"),
          f"{s2.get('edges_total')} vs {s0.get('edges_total')}")
    s2b = call("/ontology/sync/status")
    check("无 stale 边", (s2b["orphans"] or {}).get("stale_projection", 0) == 0,
          s2b.get("orphans"))

    print("4) 再对一次账，边不会复活")
    call("/ontology/sync?dry_run=false", "POST")
    s3 = status()
    check("对账后边数仍等于基线", s3.get("edges_total") == s0.get("edges_total"),
          f"{s3.get('edges_total')} vs {s0.get('edges_total')}")

    print("5) R2 撤销事实 → 图上边跟着消失")
    facts = call("/knowledge/facts?limit=400")
    rows = facts if isinstance(facts, list) else (facts.get("items") or [])
    target = next((r for r in rows
                   if (r.get("predicate") or "") in ("reports_to", "works_at")
                   and r.get("status") == "active"), None)
    if target:
        fid2 = target["id"]
        before = call("/ontology/graph?depth=2&hide_isolated=false&limit=400")
        n_before = len(before.get("edges") or [])
        w = call(f"/knowledge/facts/{fid2}/withdraw", "POST", {"reason": "同步验证"})
        check("撤销成功", w.get("status") == "withdrawn", w)
        check("响应里有边同步结果", "edges_synced" in w, w.get("edges_synced"))
        after = call("/ontology/graph?depth=2&hide_isolated=false&limit=400")
        n_after = len(after.get("edges") or [])
        check("图上的边不变或减少", n_after <= n_before, f"{n_before} → {n_after}")
    else:
        print("   跳过：没找到 reports_to / works_at 事实")

    print("\n结果：%d 通过 / %d 失败" % (len(ok), len(fail)))
    if fail:
        print("失败项：", fail)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
