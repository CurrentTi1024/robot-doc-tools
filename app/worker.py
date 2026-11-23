from functools import lru_cache

from celery import Celery

from .config import BROKER
from .service import Service

celery = Celery("robot_doc_tools", broker=BROKER)
celery.conf.update(
    task_ignore_result=True,
    worker_prefetch_multiplier=1,
    task_serializer="json",
    accept_content=["json"],
    beat_schedule={
        f"poll-{stage}": {
            "task": "app.worker.poll",
            "schedule": 2.0,
            "args": (stage,),
            "options": {"queue": stage, "expires": 5},
        }
        for stage in ("document", "checklist", "scoring")
    },
)


@lru_cache(maxsize=1)
def service():
    instance = Service()
    instance.repo.initialize()
    return instance


@celery.task(name="app.worker.poll")
def poll(stage):
    if stage not in {"document", "checklist", "scoring"}:
        raise ValueError("Invalid stage")
    instance = service()
    while instance.run_one(stage):
        pass
