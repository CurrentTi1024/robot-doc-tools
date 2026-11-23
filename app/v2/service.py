from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import logging
from subprocess import SubprocessError
from threading import Event, Thread
from uuid import uuid4

import pypdfium2 as pdfium

from app.config import CRITERIA, MAX_BYTES
from app.storage import Storage
from .models import ModelUnavailable, Settings, json_message
from .presentation import PDF_RENDER_LOCK, InvalidPresentation, extract, open_pptx, render_pdf
from .repository import MAX_ATTEMPTS, Repository
from .review import checklist, evaluate, score_item


LOG = logging.getLogger(__name__)
STAGES = {"document": ("document",), "checklist": ("checklist",), "scoring": ("judge", "summary")}


class TooLarge(ValueError):
    pass


def aggregate(panel, version):
    items = []
    for criterion in CRITERIA:
        judges = [{"judge": run["profile"], "model": run["model"], "status": run["state"],
                   "result": run["scores"].get(criterion)} for run in panel]
        scored = [row["result"] for row in judges if row["result"] and row["result"]["score"] is not None]
        values = [item["score"] for item in scored]
        full = bool(judges) and len(scored) == len(judges)
        levels = {item["level"] for item in scored}
        items.append({
            "criterion_id": criterion, "valid_judges": len(values), "expected_judges": len(judges),
            "mean_score": round(sum(values) / len(values), 2) if full else None,
            "spread": round(max(values) - min(values), 2) if len(values) >= 2 else None,
            "needs_review": not full or len(levels) > 1 or any(item["needs_review"] for item in scored),
            "judges": judges, "summary": None,
        })
    return {"rules_version": version, "criteria": items, "summary": None, "summary_status": "pending"}


def synthesize(settings, report):
    # The synopsis sees only validated findings. Numerical scores are already final.
    packet = []
    known = {}
    for item in report["criteria"]:
        judges, ids = [], set()
        for judge in item["judges"]:
            result = judge["result"]
            if result:
                judges.append({key: result[key] for key in ("level", "reason", "evidence", "needs_review")})
                ids.update(e["id"] for e in result["evidence"])
        cid = item["criterion_id"]
        known[cid] = ids
        packet.append({"criterion_id": cid, "needs_review": item["needs_review"], "findings": judges})
    instruction = (
        "Write a concise Chinese review using only the validated findings. Preserve factual disagreements "
        "and flag contradictions; never invent evidence or recalculate numeric scores. "
        'Return JSON {"criteria":[{"criterion_id":"A1","text":"...","evidence_ids":[],"needs_review":true}],'
        '"summary":"..."}, with exactly one result for each of A1,A2,A3,B1,B2,B3.'
    )
    try:
        response = json_message(settings.model(settings.summary).invoke([
            ("system", instruction), ("human", json.dumps(packet, ensure_ascii=False)),
        ]))
        rows = response["criteria"]
        text = response["summary"]
        if not isinstance(rows, list) or len(rows) != 6 or not isinstance(text, str) or not text.strip():
            raise ValueError("Incomplete summary")
        indexed = {row["criterion_id"]: row for row in rows}
        if set(indexed) != set(known):
            raise ValueError("Incorrect summary IDs")
        clean = {}
        for identifier, row in indexed.items():
            ids = row["evidence_ids"]
            if (not isinstance(ids, list) or any(not isinstance(e, str) or e not in known[identifier] for e in ids)
                    or not isinstance(row["text"], str) or not row["text"].strip()
                    or type(row["needs_review"]) is not bool):
                raise ValueError("Incorrect summary references")
            clean[identifier] = {"text": row["text"].strip()[:700],
                                 "evidence_ids": list(dict.fromkeys(ids)),
                                 "needs_review": row["needs_review"]}
    except Exception as exc:
        raise ModelUnavailable("Could not synthesize the validated scoring results") from exc
    return clean, text.strip()[:1200]


class Service:
    def __init__(self, repo=None, storage=None, settings=None, runner=evaluate,
                 renderer=render_pdf, summarizer=synthesize):
        self.repo = repo if repo is not None else Repository()
        self.storage = storage if storage is not None else Storage()
        self.settings = settings if settings is not None else Settings()
        self.runner = runner
        self.renderer = renderer
        self.summarizer = summarizer

    def submit(self, submission_id, category, body):
        if len(body) > MAX_BYTES:
            raise TooLarge("PPTX exceeds the upload limit")
        open_pptx(body)
        job = uuid4().hex
        key = f"v2/uploads/{job}.pptx"
        self.storage.put(key, body)
        self.repo.insert_upload(submission_id, category, job, key, self.settings.rules_digest,
                                self.settings.model_digest)
        return job

    @contextmanager
    def keep_lease(self, job):
        stop = Event()

        def renew():
            while not stop.wait(25):
                try:
                    self.repo.renew(job)
                except Exception:
                    LOG.warning("Could not renew lease for job %s", job["id"])

        thread = Thread(target=renew, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=1)

    def run_one(self, role):
        job = self.repo.claim(STAGES[role])
        if job is None:
            return False
        with self.keep_lease(job):
            stage = job["stage"]
            try:
                if job["exhausted"] or not self.matches_settings(job):
                    error = "worker_lease_expired" if job["exhausted"] else "configuration_changed"
                    self.fail(job, error, retry=False)
                elif stage == "document":
                    self.repo.finish(job, evidence=self.parse(job))
                elif stage == "checklist":
                    result = checklist(self.settings, self.storage, self.repo.document_for(job), self.runner)
                    self.repo.finish(job, result=result)
                elif stage == "judge":
                    self.judge(job)
                else:
                    self.summarize(job)
            except InvalidPresentation:
                self.fail(job, "invalid_presentation", retry=False)
            except ModelUnavailable:
                self.fail(job, "model_unavailable")
            except Exception as exc:
                LOG.error("V2 job=%s stage=%s exception=%s", job["id"], stage, type(exc).__name__)
                self.fail(job, f"{stage}_failed")
        return True

    def matches_settings(self, job):
        return (job["rules"] == self.settings.rules_digest
                and (job["stage"] == "document" or job["model_config"] == self.settings.model_digest))

    def parse(self, job):
        from pathlib import Path

        body = self.storage.get(job["source"])
        template = open_pptx(Path(self.settings.template).read_bytes()) if self.settings.template else None
        evidence = extract(open_pptx(body), self.settings.rules, template)
        if any(block["visual"] for page in evidence["pages"] for block in page["blocks"]):
            try:
                rendered = self.renderer(body, self.settings.soffice)
                with PDF_RENDER_LOCK, pdfium.PdfDocument(rendered) as pdf:
                    if len(pdf) != evidence["slide_count"]:
                        raise InvalidPresentation("Rendered slide count does not match the input")
            except (OSError, SubprocessError, pdfium.PdfiumError, InvalidPresentation) as exc:
                LOG.warning("V2 job=%s slide rendering unavailable: %s", job["id"], type(exc).__name__)
            else:
                key = f"v2/rendered/{job['id']}/{job['lease']}.pdf"
                self.storage.put(key, rendered)
                evidence["rendered"] = key
        return evidence

    def judge(self, job):
        previous = self.repo.existing_scores(job["id"])
        document = self.repo.document_for(job)
        for criterion in CRITERIA:
            if criterion in previous:
                continue
            result = score_item(self.settings, self.storage, document, job["category"],
                                criterion, job["profile"], self.runner)
            if not self.repo.save_score(job, result):
                return
        self.repo.finish(job)

    def report(self, job):
        parent_result = self.repo.snapshot(job["id"], "scoring")
        if parent_result and "result" in parent_result:
            return parent_result["result"]
        report = aggregate(self.repo.panel(job["id"]), self.settings.rules["version"])
        report["rules_digest"] = job["rules"]
        return report

    def summarize(self, job):
        report = self.report(job)
        if not self.repo.save_panel_result(job, report):
            return
        summaries, summary = self.summarizer(self.settings, report)
        for item in report["criteria"]:
            row = summaries[item["criterion_id"]]
            item["summary"] = row["text"]
            item["summary_evidence_ids"] = row["evidence_ids"]
            item["needs_review"] = item["needs_review"] or row["needs_review"]
        report.update(summary=summary, summary_status="completed")
        self.repo.finish(job, result=report)

    def fail(self, job, error, retry=True):
        if job["stage"] == "summary" and (not retry or job["attempts"] >= MAX_ATTEMPTS):
            report = self.report(job)
            report["summary_status"] = "failed"
            self.repo.finish(job, result=report, error=error, retry=False)
        else:
            self.repo.finish(job, error=error, retry=retry)

    def drain(self, role, concurrency=1):
        def run():
            while self.run_one(role):
                pass

        if concurrency == 1:
            run()
        else:
            with ThreadPoolExecutor(max_workers=concurrency) as workers:
                for future in [workers.submit(run) for _ in range(concurrency)]:
                    future.result()
