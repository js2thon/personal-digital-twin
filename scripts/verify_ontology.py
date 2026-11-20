#!/usr/bin/env python
"""本体层一键自检：验证双时间线、信任护栏、争议裁决、多值维度是否真的生效。

用法：
    python scripts/verify_ontology.py                 # 跑全部检查
    python scripts/verify_ontology.py --json          # 输出机器可读 JSON
    python scripts/verify_ontology.py --base http://127.0.0.1:8101

退出码：0 = 全部通过；1 = 有失败项；2 = 服务不可达。

脚本只读，不写任何数据。检查逻辑在后端 ontology.diagnostics()，
页面「本体中心 → 本体自检」看到的是同一份结论。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

try:  # Windows 控制台默认 GBK，中文会炸
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


def get(base: str, path: str, timeout: float = 20):
    req = urllib.request.Request(base.rstrip("/") + path,
                                 headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return json.loads(raw) if raw.strip() else None


def ok_mark(flag: bool) -> str:
    return "[PASS]" if flag else "[FAIL]"


def main() -> int:
    ap = argparse.ArgumentParser(description="本体层自检")
    ap.add_argument("--base", default="http://127.0.0.1:8101")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    args = ap.parse_args()
    base = args.base

    # 0. 服务可达
    try:
        health = get(base, "/healthz", timeout=5)
    except Exception as exc:  # noqa: BLE001
        print(f"服务不可达：{base}  ({exc})")
        print("先启动：start-services.bat daemon")
        return 2

    try:
        report = get(base, "/api/v1/ontology/diagnostics")
    except urllib.error.HTTPError as exc:
        print(f"自检接口返回 {exc.code}；后端可能还是旧进程，重启 uvicorn 后重试。")
        return 1

    if args.json:
        print(json.dumps({"health": health, "diagnostics": report},
                         ensure_ascii=False, indent=2))
        return 0 if report.get("summary", {}).get("failed", 1) == 0 else 1

    s = report.get("summary", {})
    print("=" * 62)
    print(" 本体层自检")
    print("=" * 62)
    print(f" 服务：{base}   时间：{report.get('generated_at', '-')}")
    print(f" 结果：{s.get('passed', 0)}/{s.get('total', 0)} 通过"
          f"{'，' + str(s.get('failed')) + ' 项失败' if s.get('failed') else '，全部通过'}")
    print("-" * 62)

    for c in report.get("checks", []):
        print(f"{ok_mark(c.get('ok'))} {c.get('name')}")
        print(f"        {c.get('detail')}")
        # 明细：判定矩阵 / 时间旅行逐条展开，这是最能说明问题的地方
        val = c.get("value")
        if c.get("id") == "decisions" and isinstance(val, list):
            for m in val:
                print(f"          {'✓' if m['ok'] else '✗'} {m['name']}"
                      f" → {m['actual']}（期望 {m['expect']}）")
        if c.get("id") == "time_travel" and isinstance(val, list):
            for t in val:
                print(f"          asof={t['asof']} → {t['actual']!r}"
                      f"（期望 {t['expect']!r}）")

    # 真实数据的时间旅行：挑一个实体，对比「现在」与「最早生效时间之前」
    print("-" * 62)
    print(" 真实数据时间旅行（抽样）")
    try:
        ents = get(base, "/api/v1/ontology/entities") or []
        pick = None
        for e in ents:
            if (e.get("fact_count") or 0) > 0:
                pick = e
                break
        if not pick:
            print(" 暂无可抽样的实体")
        else:
            eid, name = pick["id"], pick.get("canonical_name")

            def single_live(asof: str | None):
                q = f"?asof={asof}" if asof else ""
                prof = get(base, f"/api/v1/ontology/entities/{eid}/profile{q}")
                live = [d for d in (prof.get("dimensions") or [])
                        if d.get("cardinality") == "single" and d.get("current")]
                return prof, live

            now, live_now = single_live(None)
            _, live_before = single_live("2000-01-01T00:00:00")
            print(f" 实体：{name}（{now.get('counts', {}).get('facts', 0)} 条事实、"
                  f"{now.get('counts', {}).get('dimensions', 0)} 个维度）")
            print(f"  单值维度 · 现在        → {len(live_now)} 个有当前值")
            print(f"  单值维度 · 2000-01-01  → {len(live_before)} 个有当前值")
            verdict = "符合预期（当时尚未生效）" if len(live_before) < len(live_now) else "需人工确认"
            print(f"  结论：{verdict}")

            # 取出带多段历史的单值维度，直接展示取代链
            chain = next((d for d in now.get("dimensions") or []
                          if d.get("cardinality") == "single"
                          and len(d.get("history") or []) > 1), None)
            if chain:
                print(f"  取代链示例 · {chain['dimension']}：")
                for h in chain["history"]:
                    val = (h.get("value") or {}).get("value", h.get("value"))
                    span = f"{str(h.get('valid_from'))[:16]} → {str(h.get('valid_to'))[:16] or '现在'}"
                    print(f"    [{h.get('status'):<10}] {span}  {val}")
    except Exception as exc:  # noqa: BLE001
        print(f" 抽样失败：{exc}")

    print("-" * 62)
    print(" 页面体验：http://127.0.0.1:8101/  →「本体中心」→ 本体自检 / 实体画像")
    print(" 强制造一条争议（不影响既有事实）：")
    print('   curl -X POST http://127.0.0.1:8101/api/v1/ontology/observations \\')
    print('     -H "Content-Type: application/json" \\')
    print('     -d "{\\"writer\\":\\"agent\\",\\"predicate\\":\\"works_at\\",'
          '\\"entity_name\\":\\"本人\\",\\"object_value\\":{\\"value\\":\\"测试值\\"}}"')
    print("   然后在本体中心「本体争议」里采纳或驳回。")
    print("=" * 62)
    return 0 if s.get("failed", 1) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
