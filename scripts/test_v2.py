import json
import os
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from pptx import Presentation
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.oxml.ns import qn
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Inches
from reportlab.pdfgen import canvas

from app.config import CRITERIA
from app.v2.api import create_app
from app.v2.models import ModelUnavailable, Settings
from app.v2.presentation import crop_png, extract, open_pptx
from app.v2.repository import Repository
from app.v2.review import Evidence, evaluate
from app.v2.service import Service


SUBMIT = {"Authorization": "Bearer proposer"}
JUDGE = {"Authorization": "Bearer judge"}
CHECKLIST = "/v2/checklist-jobs"
SCORING = "/v2/scoring-jobs"


class MemoryStorage(dict):
    def put(self, path, data):
        self[path] = data


def proposal(*logical_pages, flow=False):
    pptx = Presentation()
    for logical in logical_pages:
        slide = pptx.slides.add_slide(pptx.slide_layouts[6])
        for i, text in enumerate((f"Workshop Section {logical}" if logical else "Appendix",
                                  "Automatic wafer handling and abnormal routing")):
            slide.shapes.add_textbox(Inches(0.25), Inches(i), Inches(9.25), Inches(0.6)).text = text
    if flow:
        a = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(3), Inches(2), Inches(1), Inches(0.5))
        b = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(6), Inches(2), Inches(1), Inches(0.5))
        a.text, b.text = "Detection", "Recheck"
        edge = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(4), Inches(2.25),
                                          Inches(6), Inches(2.25))
        edge.begin_connect(a, 3)
        edge.end_connect(b, 1)
        arrow = OxmlElement("a:tailEnd")
        arrow.set("type", "triangle")
        edge._element.spPr.get_or_add_ln().append(arrow)
        assert edge._element.spPr.ln.find(qn("a:tailEnd")) is not None
    out = BytesIO()
    pptx.save(out)
    return out.getvalue()


def pdf_fixture(number_of_pages=1):
    out = BytesIO()
    doc = canvas.Canvas(out, pagesize=(720, 540))
    for _ in range(number_of_pages):
        doc.drawString(24, 400, "Flow chart")
        doc.showPage()
    doc.save()
    return out.getvalue()


def fake_runner(settings, profile, skill, evidence, rule):
    if evidence.preferred is not None:
        evidence.read_slide(evidence.preferred)
    last = next(reversed(evidence.pages))
    blocks = evidence.read_slide(last)["blocks"]
    evidence_id = next(block["id"] for block in blocks if block["text"])
    response = {"status": "present", "evidence_ids": [evidence_id], "reason": "Verified evidence"}
    if skill == "workshop-scoring":
        high = rule["levels"][-1]
        response.update(score=high["min"], level=high["id"], needs_review=False)
    return response


def fake_summary(_, report):
    return {cid: {"text": "经核验的评语", "evidence_ids": [], "needs_review": False}
            for cid in CRITERIA}, "整体评语"


class V2Tests(unittest.TestCase):
    def test_mapping_flow_and_crop(self):
        rules = Settings().rules
        indexed = extract(open_pptx(proposal(1, 2, 3, 4, 5, 6, 7, 10)), rules)
        self.assertEqual(indexed["slide_count"], 8)
        self.assertEqual([page["page"] for page in indexed["pages"]], list(range(1, 8)))
        document = extract(open_pptx(proposal(2, 4, flow=True)), rules)
        self.assertEqual([(p["page"], p["template_page"]) for p in document["pages"]], [(1, 2), (2, 4)])
        flow = next(block for block in document["pages"][1]["blocks"] if block["kind"] == "flow")
        edge = flow["edges"][0]
        self.assertEqual((edge["direction"], edge["provenance"]), ("forward", "explicit"))
        self.assertNotEqual(edge["source"], edge["target"])
        self.assertTrue(crop_png(pdf_fixture(2), 2, [250, 250, 750, 750]).startswith(b"\x89PNG"))

    def test_two_api_routes_and_recovery(self):
        calls = []
        summary_calls = []

        def runner(settings, profile, skill, evidence, rule):
            if skill == "workshop-scoring":
                calls.append((profile, rule["id"]))
                if ("judge_b", "A2") == calls[-1] and calls.count(calls[-1]) == 1:
                    raise ModelUnavailable("simulated timeout")
            return fake_runner(settings, profile, skill, evidence, rule)

        def summarizer(settings, report):
            summary_calls.append(True)
            if len(summary_calls) == 1:
                raise ModelUnavailable("summary timeout")
            return fake_summary(settings, report)

        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            "RW_TOKEN_SUBMIT": "proposer", "RW_TOKEN_JUDGE": "judge",
        }):
            repo = Repository(str(Path(directory) / "test.db"))
            service = Service(repo=repo, storage=MemoryStorage(), runner=runner,
                              summarizer=summarizer, renderer=lambda *_: pdf_fixture(2))
            app = create_app(service)
            self.assertEqual(set(app.openapi()["paths"]), {CHECKLIST, SCORING})
            with TestClient(app) as client:
                data = {"submission_id": "T1", "category": "A"}
                files = {"pptx": ("proposal.pptx", proposal(1, 3, flow=True))}
                self.assertEqual(client.post(CHECKLIST, data=data, files=files).status_code, 401)
                response = client.post(CHECKLIST, data=data, files=files, headers=SUBMIT)
                self.assertEqual(response.status_code, 202, response.text)
                upload = response.json()["job_id"]
                args = {"json": {"upload_job_ids": [upload]}, "headers": JUDGE}
                self.assertEqual(client.post(SCORING, **args).status_code, 409)
                service.drain("document")
                service.drain("checklist")
                checklist = client.post(CHECKLIST, params={"job_id": upload}, headers=SUBMIT).json()
                self.assertEqual(checklist["status"], "completed")
                self.assertEqual(checklist["result"]["items"][0]["evidence"][0]["page"], 2)
                response = client.post(SCORING, **args)
                self.assertEqual(response.status_code, 202, response.text)
                job_id = response.json()["job_ids"][0]
                self.assertEqual(client.post(SCORING, **args).json()["job_ids"], [job_id])
                self.assertEqual(client.post(SCORING, json={"job_id": job_id}, headers=SUBMIT).status_code, 401)
                service.drain("scoring", 2)
                progress = client.post(SCORING, json={"job_id": job_id}, headers=JUDGE).json()
                self.assertEqual(sorted(judge["criteria_done"] for judge in progress["judges"]), [1, 6])
                with repo.transaction(write=True) as q:
                    q("UPDATE rw_v2_jobs SET next_at=0 WHERE state='queued'")
                service.drain("scoring", 2)
                progress = client.post(SCORING, json={"job_id": job_id}, headers=JUDGE).json()
                self.assertEqual(progress["result"]["summary_status"], "pending")
                with repo.transaction(write=True) as q:
                    q("UPDATE rw_v2_jobs SET next_at=0 WHERE state='queued'")
                service.drain("scoring", 2)
                output = client.post(SCORING, json={"job_id": job_id}, headers=JUDGE).json()
                self.assertEqual(output["status"], "completed")
                self.assertEqual(output["result"]["summary_status"], "completed")
                self.assertTrue(all(item["valid_judges"] == 2 and item["mean_score"] is not None
                                    for item in output["result"]["criteria"]))
                self.assertEqual(calls.count(("judge_b", "A1")), 1)
                self.assertEqual(calls.count(("judge_b", "A2")), 2)

    def test_real_deep_agents_skill_and_image_tool_with_mock_gateway(self):
        from langchain_openai import ChatOpenAI

        gateway_calls = []

        def gateway(request):
            payload = json.loads(request.content)
            messages = payload["messages"]
            images = [part for entry in messages if isinstance(entry.get("content"), list)
                      for part in entry["content"] if isinstance(part, dict) and part.get("type") == "image_url"]
            gateway_calls.append("vision" if images else "agent")
            if images:
                self.assertTrue(images[0]["image_url"]["url"].startswith("data:image/png;base64,"))
                result = '{"status":"readable","observation":"Flow ends in a recheck."}'
                message = {"role": "assistant", "content": result}
            else:
                tools = {item["function"]["name"] for item in payload.get("tools", [])}
                self.assertIn("read_file", tools)
                self.assertIn("inspect_visual", tools)
                self.assertNotIn("write_file", tools)
                self.assertNotIn("task", tools)
                tool_answers = [entry["content"] for entry in messages if entry["role"] == "tool"]
                if len(tool_answers) == 3:
                    self.assertIn("Flow ends in a recheck", str(tool_answers[-1]))
                    result = '{"status":"present","evidence_ids":["p1-flow"],"reason":"Verified recheck"}'
                    message = {"role": "assistant", "content": result}
                else:
                    name, arguments = [
                        ("read_file", {"file_path": "/workshop-checklist/SKILL.md"}),
                        ("read_slide", {"page": 1}),
                        ("inspect_visual", {"evidence_id": "p1-flow", "question": "Where does it end?"}),
                    ][len(tool_answers)]
                    if tool_answers:
                        self.assertIn("preferred slide", str(tool_answers[0]))
                    message = {"role": "assistant", "content": None, "tool_calls": [{
                        "id": f"call_{len(tool_answers)}", "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)},
                    }]}
            return httpx.Response(200, json={"id": "test", "created": 0, "model": "test",
                                              "object": "chat.completion", "choices": [{
                                                  "index": 0, "message": message,
                                                  "finish_reason": "tool_calls" if "tool_calls" in message else "stop",
                                              }]})

        settings = Settings()
        with (httpx.Client(transport=httpx.MockTransport(gateway), trust_env=False) as http,
              patch.dict(os.environ, {"HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "",
                                      "http_proxy": "", "https_proxy": "", "all_proxy": ""})):
            model = ChatOpenAI(model="test", api_key="test", base_url="http://test.invalid/v1",
                               max_retries=0, http_client=http)
            settings.model = lambda _: model
            document = extract(open_pptx(proposal(1, flow=True)), settings.rules)
            document["rendered"] = "fixture.pdf"
            evidence = Evidence(document, MemoryStorage({"fixture.pdf": pdf_fixture()}), settings, "qwen", 1)
            result = evaluate(settings, "qwen", "workshop-checklist", evidence, {"requirement": "has recheck"})
            self.assertEqual(result["status"], "present", result)
            self.assertIn("p1-flow", evidence.accessed)
            self.assertEqual(gateway_calls.count("vision"), 1)


if __name__ == "__main__":
    unittest.main()
