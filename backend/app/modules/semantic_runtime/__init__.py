"""统一语义运行时（SIL B2 / 蓝图第 9 章）。

对上：Agent 与业务侧唯一的知识获取入口（蓝图 20.2 第 8 条）。
对下：只编排现有能力，不重写检索、图谱、策略与证据逻辑。

模块分层：
    context.py   6 项强制上下文（AC-01）
    service.py   5 阶段运行时流水线 + 四通道混合检索（RP / AC / DC / EC）
    api/v1/semantic.py  薄路由，只做参数校验与错误映射

设计约束：
1. 不改动任何现有模块的函数签名；
2. 所有返回事实必须经过 app.visibility 白名单（G2 fail-closed）；
3. 任一通道不可用必须显式 degraded 标注，不静默返回部分结果（AC-06）；
4. 单 worker 进程内缓存，不引入跨进程共享状态。
"""
from .context import SemanticContext, from_request, system_context  # noqa: F401
from . import service  # noqa: F401
