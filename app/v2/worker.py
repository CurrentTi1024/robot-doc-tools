import argparse
from functools import lru_cache
import logging
import os
import time

from celery import Celery

from app.config import BROKER
from .service import STAGES, Service


celery = Celery("robot_doc_tools_v2", broker=BROKER)
celery.conf.update(
    task_ignore_result=True,
    worker_prefetch_multiplier=1,
    task_serializer="json",
    accept_content=["json"],
    beat_schedule={f"poll-v2-{role}": {
        "task": "app.v2.worker.poll", "schedule": 2.0, "args": (role,),
        "options": {"queue": f"v2-{role}", "expires": 5},
    } for role in STAGES},
)


@lru_cache(maxsize=1)
def service():
    instance = Service()
    instance.repo.initialize()
    return instance


def concurrency(role):
    return max(1, int(os.getenv("RW_V2_SCORING_CONCURRENCY", "2"))) if role == "scoring" else 1


@celery.task(name="app.v2.worker.poll")
def poll(role):
    service().drain(role, concurrency(role))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=STAGES)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    instance = service()
    while True:
        instance.drain(args.role, concurrency(args.role))
        time.sleep(1)


if __name__ == "__main__":
    main()
