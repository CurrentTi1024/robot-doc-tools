import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app.config import DATA
from app.evaluation import accepted_score, score
from app.llm import ModelUnavailable, settings
from app.ocr_process import run_ocr
from app.repository import Repository
from app.rules import load_rules
from app.service import Service


class JobConsistencyTests(unittest.TestCase):
    def setUp(self):
        DATA.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=DATA)
        self.addCleanup(directory.cleanup)
        self.repo = Repository(str(Path(directory.name) / "cases.db"))
        self.repo.initialize()

    def new_job(self, revision=1):
        return self.repo.submit("fixture", revision, "A", f"digest-{revision}", "fixture.pdf", "rules")

    def test_concurrent_submissions_are_idempotent(self):
        with ThreadPoolExecutor(max_workers=4) as callers:
            ids = list(callers.map(lambda _: self.new_job(), range(8)))
        self.assertEqual(len(set(ids)), 1)

    def test_expired_lease_cannot_publish_after_takeover(self):
        identifier = self.new_job()
        stale = self.repo.claim("document")
        with self.repo.transaction(write=True) as query:
            query("UPDATE jobs SET lease_until=0 WHERE id=?", identifier)
        current = self.repo.claim("document")
        self.assertNotEqual(stale["lease"], current["lease"])
        self.repo.extend_lease(stale)
        self.repo.finish(stale, evidence='{"engine":"stale"}')
        self.assertEqual(self.repo.job(identifier)["state"], "running")
        self.repo.finish(current, evidence='{"engine":"fresh"}')
        next_stage = self.repo.claim("checklist")
        self.assertEqual(next_stage["document"]["evidence"], '{"engine":"fresh"}')

    def test_old_revision_cannot_publish(self):
        identifier = self.new_job()
        stale = self.repo.claim("document")
        self.new_job(revision=2)
        self.repo.finish(stale, evidence='{"engine":"stale"}')
        self.assertEqual(self.repo.job(identifier)["state"], "superseded")
        latest = self.repo.claim("document")
        self.assertIsNone(latest["document"]["evidence"])

    def test_only_current_failed_job_can_be_retried(self):
        identifier = self.new_job()
        self.repo.finish(self.repo.claim("document"), error="unavailable", retry=False)
        self.assertFalse(self.repo.retry_failed(identifier, "wrong-rules"))
        with ThreadPoolExecutor(max_workers=4) as callers:
            results = list(callers.map(lambda _: self.repo.retry_failed(identifier, "rules"), range(8)))
        self.assertEqual(results.count(True), 1)
        retried = self.repo.claim("document")
        self.assertEqual(retried["id"], identifier)
        self.assertEqual(retried["attempts"], 1)
        self.repo.finish(retried, result={"done": True})
        self.assertFalse(self.repo.retry_failed(identifier, "rules"))

    def test_ocr_timeout_requeues_and_releases_worker(self):
        service = Service(repo=self.repo)
        identifier = self.repo.submit("ocr", 1, "A", "digest", "example.pdf", service.rules_digest)
        real_run = subprocess.run

        def slow_process(_, **kwargs):
            return real_run([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)

        with patch("app.ocr_process.OCR_TIMEOUT", 0.2), patch("app.ocr_process.subprocess.run", slow_process):
            self.assertTrue(service.run_one("document"))
        job = self.repo.job(identifier)
        self.assertEqual(job["state"], "queued")
        self.assertEqual(job["error"], "document_processing_timeout")
        other = self.repo.submit("next", 1, "A", "digest", "example.pdf", service.rules_digest)
        self.assertEqual(self.repo.claim("document")["id"], other)


class ScoreValidationTests(unittest.TestCase):
    rule = {"levels": [{"id": "pass", "min": 60, "max": 80}]}
    evidence = {"p2-b1": {"id": "p2-b1", "text": "Traceable evidence", "page": 2}}

    def grade(self, score, citations):
        return accepted_score({"score": score, "level": "pass", "reasons": [
            {"text": "Recognized traceable evidence", "evidence_ids": citations}
        ]}, self.rule, self.evidence)

    def test_rejects_score_outside_the_chosen_band(self):
        self.assertIsNone(self.grade(81, ["p2-b1"]))
        self.assertIsNone(self.grade(float("nan"), ["p2-b1"]))

    def test_rejects_fabricated_evidence_reference(self):
        self.assertIsNone(self.grade(70, ["p12-b1"]))
        self.assertEqual(self.grade(70, ["p2-b1"])["evidence_pages"], [2])

    def test_confirmed_absence_uses_only_configured_score(self):
        rules, _ = load_rules()
        rule = rules["rubrics"]["A"]["A1"]
        rule["missing_score"] = min(band["min"] for band in rule["levels"])
        pages = [{"pdf_page": 1, "blocks": [dict(self.evidence["p2-b1"], type="text", bbox=None)]}]

        def llm(_, data):
            return {"conditions": [{"index": index, "status": "unsupported", "evidence_ids": []}
                                   for index, _ in enumerate(data["conditions"])]}

        result = score({"pages": pages}, rules, "A", llm)["criteria"]
        self.assertEqual(result[0]["score"], rule["missing_score"])
        self.assertFalse(result[0]["needs_review"])
        self.assertTrue(result[1]["needs_review"])
        pages.append({"pdf_page": 2, "blocks": []})
        uncertain = score({"pages": pages}, rules, "A", llm)["criteria"][0]
        self.assertIsNone(uncertain["score"])
        self.assertTrue(uncertain["needs_review"])


class LLMConfigurationTests(unittest.TestCase):
    def test_formal_rules_cannot_inherit_a_demo_model(self):
        DATA.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=DATA) as directory:
            demo = Path(directory) / "demo.txt"
            demo.write_text("https://example.invalid/\ndeepseek-fixture\nsk-fixture\n", encoding="utf-8")
            env = {"RW_LLM_FILE": str(demo), "RW_LLM_URL": "", "RW_LLM_MODEL": "", "RW_LLM_KEY": ""}
            with patch.dict("os.environ", env):
                self.assertEqual(settings(allow_local_file=True)[1], "deepseek-fixture")
                with self.assertRaises(ModelUnavailable):
                    settings(allow_local_file=False)
            with patch.dict("os.environ", {**env, "RW_LLM_MODEL": "deepseek-fixture"}):
                with self.assertRaises(ModelUnavailable):
                    settings(allow_local_file=True)


if __name__ == "__main__":
    unittest.main()
