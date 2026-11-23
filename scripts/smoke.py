import json
from pathlib import Path

from app.config import CRITERIA, DATA
from app.repository import Conflict, Repository
from app.service import Service
from scripts.generate_sample import generate


class FixtureModel:
    def complete(self, messages):
        return self(messages[0]["content"], json.loads(messages[-1]["content"]))

    def __call__(self, instruction, data):
        evidence = data["evidence"]
        if "each rubric condition" in instruction:
            return {"conditions": [
                {"index": index, "status": "supported", "evidence_ids": [evidence[0]["id"]]}
                for index, _ in enumerate(data["conditions"])
            ]}
        if "Pick a level" in instruction:
            score = 30 if data["criterion"]["name"].endswith("B2") else 85
            return {"score": score, "level": "basic" if score == 30 else "supported",
                    "reasons": [{"text": "列出了作业目标。", "evidence_ids": [evidence[0]["id"]]}]}
        match = next((block for block in evidence if "inspect the pick station" in block["text"]), None)
        return {"status": "present" if match else "missing", "evidence_ids": [match["id"]] if match else []}


def fixture_agent():
    from app.agent import EvaluationAgent
    return EvaluationAgent(transport=FixtureModel())


def execute(repo, fixture, *, live_ocr=False, live_llm=False):
    number = fixture.parent.name
    identifier = f"DEMO-{number}"
    service = Service(repo=repo, smoke=not live_ocr, llm_factory=None if live_llm else fixture_agent)
    repo.initialize()
    pdf = fixture.read_bytes()
    job = service.submit(identifier, 1, "A", pdf)
    assert job == service.submit(identifier, 1, "A", pdf)
    assert service.run_one("document") and repo.job(job)["state"] == "queued", repo.job(job)
    assert service.run_one("checklist")
    check = service.result(identifier, "checklist")
    assert check is not None, repo.job(job)
    indexed = {item["item_id"]: item for item in check["items"]}
    assert len(indexed) == 14
    assert indexed["P3-LABEL"]["status"] == "missing", indexed["P3-LABEL"]
    assert indexed["P5-LABEL"]["evidence_pages"] == [7], indexed["P5-LABEL"]
    evaluation_job, = service.score_jobs([(identifier, 1)])
    assert service.run_one("scoring")
    evaluation = service.result(identifier, "scoring")
    assert evaluation is not None, repo.job(evaluation_job)
    assert [item["id"] for item in evaluation["criteria"]] == list(CRITERIA)
    assert evaluation["summary"]["text"]
    if not live_llm:
        assert [item["score"] for item in evaluation["criteria"]] == [85, 85, 85, 85, 30, 85]
        assert evaluation["summary"]["strengths"] and evaluation["summary"]["gaps"]
    for name, result in (("checklist", check), ("evaluation", evaluation)):
        (fixture.parent / f"{name}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    revised = generate(fixture.parent / "revision2.pdf").read_bytes()
    service.submit(identifier, 2, "A", revised)
    assert repo.job(job)["state"] == "superseded" and repo.job(evaluation_job)["state"] == "superseded"
    assert service.result(identifier, "checklist") is None and service.result(identifier, "scoring") is None
    try:
        service.submit(identifier, 1, "A", pdf)
    except Conflict:
        pass
    else:
        raise AssertionError("Old revision accepted")
    print(f"PASS ({check['parser']}, {'Deep Agents + live DeepSeek' if live_llm else 'fixture LLM'}): 7 PDF pages, "
          f"{len(indexed)} checks, six scores/flags, overall summary, idempotency and revision isolation")
    print(f"Outputs: {fixture.parent}")


if __name__ == "__main__":
    import argparse
    from tempfile import mkdtemp

    parser = argparse.ArgumentParser()
    parser.add_argument("--live-ocr", action="store_true")
    parser.add_argument("--live-llm", action="store_true")
    args = parser.parse_args()
    DATA.mkdir(parents=True, exist_ok=True)
    output = Path(mkdtemp(prefix="smoke-", dir=DATA))
    fixture = generate(output / "sample.pdf", omit_page_label=3, move_page_label=5)
    execute(Repository(str(output / "smoke.db")), fixture, live_ocr=args.live_ocr, live_llm=args.live_llm)
