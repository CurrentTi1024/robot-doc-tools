import hashlib
import json
import logging
import re
from subprocess import TimeoutExpired
from functools import partial
from threading import Event, Thread

from .config import MAX_BYTES
from .document import extract, validate_pdf
from .evaluation import checklist, score
from .llm import ModelUnavailable
from .ocr_process import run_ocr
from .repository import Repository
from .rules import load_rules
from .storage import Storage

LOG = logging.getLogger(__name__)


class TooLarge(ValueError):
    pass


def evaluation_agent(*, allow_local_file=True):
    from .agent import EvaluationAgent
    from .llm import DeepSeek
    return EvaluationAgent(transport=DeepSeek(allow_local_file=allow_local_file))


class Service:
    def __init__(self, repo=None, storage=None, llm_factory=None, smoke=False, rules_path=None):
        self.repo = repo if repo is not None else Repository()
        self.storage = storage if storage is not None else Storage()
        self.smoke = smoke
        self.rules, self.rules_digest = load_rules(rules_path) if rules_path is not None else load_rules()
        self.llm_factory = llm_factory if llm_factory is not None else partial(
            evaluation_agent, allow_local_file=self.rules["version"] == "demo-v1"
        )

    def submit(self, identifier, revision, category, body):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", identifier):
            raise ValueError("Invalid submission ID")
        if type(revision) is not int or not 1 <= revision <= 2_147_483_647 or category not in ("A", "B", "C"):
            raise ValueError("Invalid revision or category")
        if len(body) > MAX_BYTES:
            raise TooLarge("PDF exceeds upload limit")
        validate_pdf(body)
        digest = hashlib.sha256(body).hexdigest()
        path = f"pdf/{digest}.pdf"
        self.storage.put(path, body)
        return self.repo.submit(identifier, revision, category, digest, path, self.rules_digest)

    def score_jobs(self, entries):
        return self.repo.score(entries, self.rules_digest)

    def result(self, identifier, operation):
        return self.repo.result(identifier, operation, self.rules_digest)

    def run_one(self, stage):
        job = self.repo.claim(stage)
        if not job:
            return False
        completed = Event()

        def renew():
            while not completed.wait(60):
                try:
                    self.repo.extend_lease(job)
                except Exception as exc:
                    LOG.error("job=%s lease renewal failed: %s", job["id"], type(exc).__name__)

        heartbeat = Thread(target=renew, daemon=True)
        heartbeat.start()
        try:
            return self._run_claimed(job, stage)
        finally:
            completed.set()
            heartbeat.join(timeout=1)

    def _run_claimed(self, job, stage):
        doc = job["document"]
        if doc is None:
            return True
        if job["rules"] != self.rules_digest:
            self.repo.finish(job, error="rule_version_unavailable", retry=False)
            return True
        try:
            if stage == "document":
                namespace = f"evidence/{doc['digest']}/{job['lease']}"
                if self.smoke:
                    evidence = extract(self.storage.get(doc["pdf"]), self.rules, self.storage, namespace, smoke=True)
                else:
                    evidence = run_ocr(doc["pdf"], self.rules, namespace)
                self.repo.finish(job, evidence=json.dumps(evidence, ensure_ascii=False))
                return True

            evidence = json.loads(doc["evidence"])
            client = None

            def call_llm(instruction, data):
                nonlocal client
                if client is None:
                    client = self.llm_factory()
                return client(instruction, data)

            if stage == "checklist":
                result = checklist(evidence, self.rules, call_llm)
            else:
                result = score(evidence, self.rules, doc["category"], call_llm)
                result.update(category=doc["category"], rubric_version=self.rules["version"])
            result.update(submission_id=job["submission_id"], revision=job["revision"],
                          rules_version=self.rules["version"], rules_sha256=self.rules_digest,
                          parser=evidence["engine"])
            self.repo.finish(job, result=result)
        except TimeoutExpired:
            self.repo.finish(job, error="document_processing_timeout")
        except ModelUnavailable:
            self.repo.finish(job, error="llm_unavailable")
        except Exception as exc:
            LOG.error("job=%s stage=%s exception=%s", job["id"], stage, type(exc).__name__)
            self.repo.finish(job, error=f"{stage}_processing_failed")
        return True
