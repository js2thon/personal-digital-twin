"""Celery 应用与任务队列。"""
import os

from celery import Celery

from app.config import settings

celery_app = Celery(
    "twin",
    broker=settings.redis_url,
    backend=settings.redis_url,
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # 本地演示模式：CELERY_TASK_ALWAYS_EAGER=1 时任务在 API 进程内同步执行
    task_always_eager=os.environ.get("CELERY_TASK_ALWAYS_EAGER", "0") == "1",
    task_eager_propagates=False,
)

# 导入任务模块以完成注册
from app.workers import pipeline_tasks  # noqa: E402,F401
