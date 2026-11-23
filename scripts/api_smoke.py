import json
import os
from pathlib import Path
from secrets import token_urlsafe
from tempfile import mkdtemp
from time import perf_counter

from fastapi.testclient import TestClient

from app import api
from app.config import DATA
from app.repository import Repository
from app.service import Service
from scripts.generate_sample import generate
from scripts.smoke import fixture_agent


def run(live_ocr, live_llm):
    started = perf_counter()
    DATA.mkdir(parents=True, exist_ok=True)
    output = Path(mkdtemp(prefix="api-", dir=DATA))
    fixture = generate(output / "sample.pdf", omit_page_label=3, move_page_label=5)
    os.environ["RW_TOKEN_SUBMIT"] = token_urlsafe()
    os.environ["RW_TOKEN_JUDGE"] = token_urlsafe()
    user = {"Authorization": "Bearer " + os.environ["RW_TOKEN_SUBMIT"]}
    judge = {"Authorization": "Bearer " + os.environ["RW_TOKEN_JUDGE"]}
    worker = Service(repo=Repository(str(output / "api.db")),
                     smoke=not live_ocr, llm_factory=None if live_llm else fixture_agent)
    api.service = worker
    with TestClient(api.app) as client:
        assert client.get("/health/ready").status_code == 200
        assert client.get("/openapi.json").status_code == 200
        form = {"submission_id": "API-SMOKE", "category": "A", "revision": "1", "request_id": "api-smoke"}
        batch = {"submissions": [{"submission_id": "API-SMOKE", "revision": 1}]}
        assert client.post("/v1/checklist-jobs", content=b"untrusted upload").status_code == 401
        assert client.post("/v1/checklist-jobs", content=b"large", headers={
            **user, "Content-Length": str(api.MAX_BYTES + 100_000),
        }).status_code == 413
        assert client.post("/v1/checklist-jobs", headers=user, data=form,
                           files={"pdf": ("invalid.pdf", b"invalid", "application/pdf")}).status_code == 422
        assert client.post("/v1/scoring-jobs", headers=judge, json=batch).status_code == 409
        res = client.post("/v1/checklist-jobs", headers=user, data=form,
                          files={"pdf": ("sample.pdf", fixture.read_bytes(), "application/pdf")})
        assert res.status_code == 202, res.text
        job = res.json()["job_id"]
        assert client.get(f"/v1/jobs/{job}", headers=user).json()["state"] == "queued"
        retry = f"/v1/jobs/{job}/retry"
        assert client.post(retry).status_code == 401
        assert client.post(retry, headers=judge).status_code == 404
        assert client.post(retry, headers=user).status_code == 409
        worker.repo.finish(worker.repo.claim("document"), error="test_failure", retry=False)
        resumed = client.post(retry, headers=user)
        assert resumed.status_code == 202 and resumed.json()["job_id"] == job
        assert worker.run_one("document")
        assert worker.run_one("checklist")
        assert client.get(f"/v1/jobs/{job}", headers=user).json()["state"] == "completed", worker.repo.job(job)
        route = "/v1/submissions/API-SMOKE"
        check = client.get(route + "/checklist", headers=user).json()
        assert len(check["items"]) == 14
        indexed = {item["item_id"]: item for item in check["items"]}
        assert indexed["P3-LABEL"]["status"] == "missing", indexed["P3-LABEL"]
        assert indexed["P5-LABEL"]["evidence_pages"] == [7], indexed["P5-LABEL"]
        res = client.post("/v1/scoring-jobs", headers=judge, json=batch)
        assert res.status_code == 202, res.text
        job = res.json()["job_ids"][0]
        assert client.get(f"/v1/jobs/{job}", headers=user).status_code == 404
        assert client.get(route + "/evaluation", headers=user).status_code == 401
        assert worker.run_one("scoring")
        res = client.get(route + "/evaluation", headers=judge)
        assert res.status_code == 200, worker.repo.job(job)
        evaluation = res.json()
        assert len(evaluation["criteria"]) == 6 and evaluation["summary"]["text"]
        if live_ocr:
            assert evaluation["parser"] == "PaddleOCR-VL-1.6"
        if live_llm:
            assert all(item["score"] is not None and item["evidence_pages"] for item in evaluation["criteria"])
        for name, value in (("checklist", check), ("evaluation", evaluation)):
            (output / f"{name}.json").write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"PASS API, parser={evaluation['parser']}, live_llm={live_llm}, checklist={check['summary']}, "
              f"numeric_scores={sum(item['score'] is not None for item in evaluation['criteria'])}, "
              f"elapsed_s={perf_counter() - started:.1f}")
        print(f"Outputs: {output}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--live-ocr", action="store_true")
    parser.add_argument("--live-llm", action="store_true")
    parser.add_argument("--repeat", type=int, choices=range(1, 11), default=1)
    args = parser.parse_args()
    for attempt in range(1, args.repeat + 1):
        print(f"Run {attempt}/{args.repeat}", flush=True)
        run(args.live_ocr, args.live_llm)
