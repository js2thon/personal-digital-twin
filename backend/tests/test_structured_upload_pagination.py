"""上传快照按页读取回归：默认 10 行 + 翻页，且测试自清理。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app, client=("127.0.0.1", 12345), raise_server_exceptions=False)


def test_upload_rows_pagination_default_ten():
    csv = "emp_no,full_name\n" + "\n".join(
        f"E{i:03d},姓名{i}" for i in range(1, 26)
    ) + "\n"
    up = client.post("/api/v1/structured/uploads",
                     files={"file": ("rows.csv", csv.encode("utf-8"), "text/csv")})
    assert up.status_code == 200, up.text
    uid = up.json()["upload_id"]
    try:
        p1 = client.get(f"/api/v1/structured/uploads/{uid}/rows", params={"page": 1})
        assert p1.status_code == 200, p1.text
        j1 = p1.json()
        assert j1["total"] == 25 and j1["pages"] == 3
        assert j1["page_size"] == 10 and len(j1["rows"]) == 10
        assert j1["rows"][0]["emp_no"] == "E001"

        p3 = client.get(f"/api/v1/structured/uploads/{uid}/rows", params={"page": 3})
        assert p3.status_code == 200, p3.text
        j3 = p3.json()
        assert len(j3["rows"]) == 5
        assert j3["rows"][0]["emp_no"] == "E021"
    finally:
        # 真实库测试必须自清理
        d = client.delete(f"/api/v1/structured/uploads/{uid}")
        assert d.status_code == 200, d.text
