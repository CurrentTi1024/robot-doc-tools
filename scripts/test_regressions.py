import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app.config import DATA
from app.evaluation import accepted_score
from app.llm import ModelUnavailable, settings
from app.repository import Repository


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
