"""语义基础设施层评估模块（SIL P0-4 / GV-02 / SIL-A10 / SIL-A12）。

- dataset.py   固定基准集 golden-v1（只读实库快照固化，带指纹）
- metrics.py   五组指标纯函数，不设绝对性能阈值
- service.py   探针运行器、版本对照、门禁判定、报告落盘与 Markdown 渲染

对外统一从 `app.modules.evaluation.service` 调用；本包不修改任何业务数据。
"""
from . import dataset, metrics, service  # noqa: F401

__all__ = ["dataset", "metrics", "service"]
