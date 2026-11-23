import math
import re
from collections import Counter

from .config import CRITERIA
from .document import normalized

MAX_CONTEXT = 80_000


def map_pages(evidence, rules):
    candidates = []
    for page in evidence["pages"]:
        content = normalized(" ".join(block["text"] for block in page["blocks"]))
        matches = [rule["page"] for rule in rules["pages"]
                   if any(normalized(anchor) in content for anchor in rule["anchors"])]
        candidates.append(matches[0] if len(matches) == 1 else None)
    counts = Counter(candidates)
    return {page["pdf_page"]: candidate if counts[candidate] == 1 else None
            for page, candidate in zip(evidence["pages"], candidates)}


def blocks_from(evidence):
    return {block["id"]: {"id": block["id"], "page": page["pdf_page"], "type": block["type"],
                          "text": block["text"], "bbox": block["bbox"]}
            for page in evidence["pages"] for block in page["blocks"]}


def evidence_packet(blocks):
    packet = []
    remaining = MAX_CONTEXT
    for block in blocks.values():
        if block["text"].strip():
            length = len(block["text"])
            if length > remaining:
                return packet, False
            packet.append(block)
            remaining -= length
    return packet, True


def validated_ids(value, blocks):
    if not isinstance(value, list) or any(not isinstance(item, str) or
            item not in blocks or not blocks[item]["text"].strip() for item in value):
        return None
    return list(dict.fromkeys(value))


def incomplete_pages(evidence):
    return any(
        not any(block["text"].strip() for block in page["blocks"])
        or any(block["type"] in {"image", "figure", "chart", "diagram"}
               and not block["text"].strip() for block in page["blocks"])
        for page in evidence["pages"]
    )


def checklist(evidence, rules, llm):
    blocks = blocks_from(evidence)
    mapping = map_pages(evidence, rules)
    incomplete = incomplete_pages(evidence)
    results = []
    for rule in rules["checklist"]:
        expected = {key: block for key, block in blocks.items() if mapping[block["page"]] == rule["page"]}
        status, ids = "uncertain", []
        if rule["kind"] == "literal":
            terms = [normalized(term) for term in rule["terms"] if normalized(term)]
            ids = [key for key, block in (expected | blocks).items()
                   if any(re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])",
                                    normalized(block["text"])) for term in terms)]
            status = "present" if ids else "missing"
        else:
            first, _ = evidence_packet(expected)
            reply = _check_item(llm, rule, first) if first else {}
            valid = validated_ids(reply.get("evidence_ids"), {b["id"]: b for b in first})
            if reply.get("status") == "present" and valid:
                status, ids = "present", valid
            else:
                full, complete = evidence_packet(expected | blocks)
                reply = _check_item(llm, rule, full) if full else {}
                valid = validated_ids(reply.get("evidence_ids"), {b["id"]: b for b in full})
                if reply.get("status") == "present" and valid:
                    status, ids = "present", valid
                elif reply.get("status") == "missing" and complete and valid == []:
                    status = "missing"

        in_expected = [key for key in ids if key in expected]
        if in_expected:
            ids = in_expected
        elif ids and rules["cross_page"] != "allow":
            status = "missing" if rules["cross_page"] == "require_expected_page" and expected else "uncertain"
        if status == "missing" and not ids:
            if incomplete or (not expected and rules["cross_page"] == "require_expected_page"):
                status = "uncertain"
        results.append({
            "item_id": rule["id"], "title": rule["title"], "template_page": rule["page"],
            "status": status, "evidence_pages": sorted({blocks[key]["page"] for key in ids}),
            "hint": rule.get("hint", rule["requirement"]) if status != "present" else None,
        })
    counts = Counter(item["status"] for item in results)
    return {"status": "completed", "items": results,
            "summary": {status: counts[status] for status in ("present", "missing", "uncertain")}}


def _check_item(llm, rule, packet):
    return llm(
        "Decide whether the required content is actually supplied. A heading or keyword "
        "alone is insufficient. Output JSON {status: present|missing|uncertain, evidence_ids: []}. "
        "Use present only with directly supporting evidence IDs. Use uncertain when unclear.",
        {"requirement": rule["requirement"], "evidence": packet},
    )


def score(evidence, rules, category, llm):
    blocks = blocks_from(evidence)
    packet, complete = evidence_packet(blocks)
    absence_can_be_scored = complete and not incomplete_pages(evidence)
    supplied = {block["id"]: block for block in packet}
    findings = []
    for identifier in CRITERIA:
        rule = rules["rubrics"][category][identifier]
        accepted = None
        if packet:
            conditions = llm(
                "Assess each rubric condition against supplied evidence. Output JSON "
                "{conditions:[{index:0,status:supported|unsupported|uncertain,evidence_ids:[]}]}. "
                "Return each zero-based index exactly once. Cite IDs for supported claims.",
                {"conditions": rule["conditions"], "evidence": packet},
            ).get("conditions")
            if valid_conditions(conditions, rule, supplied):
                accepted_ids = {key for item in conditions if item["status"] == "supported"
                                for key in item["evidence_ids"]}
                if (absence_can_be_scored and "missing_score" in rule
                        and all(c["status"] == "unsupported" for c in conditions)):
                    value = rule["missing_score"]
                    band = next(level for level in rule["levels"] if level["min"] <= value <= level["max"])
                    accepted = {"score": value, "level": band["id"], "evidence_pages": [],
                                "reasons": [{"text": "未找到支持该评分条件的证据，按评分规则给分。",
                                             "evidence_ids": [], "evidence_pages": []}],
                                "questions": [], "needs_review": False}
                elif accepted_ids and complete and all(c["status"] != "uncertain" for c in conditions):
                    context = [supplied[key] for key in sorted(accepted_ids)]
                    for _ in range(2):
                        proposal = llm(
                            "Use the condition findings and grading rubric. Pick a level, then a numeric score "
                            "within that level. Use only supplied evidence for reasons. Output JSON "
                            "{level,score,reasons:[{text,evidence_ids:[]}]}. Use the same language as the rubric.",
                            {"criterion": rule, "conditions": conditions, "evidence": context},
                        )
                        accepted = accepted_score(proposal, rule, {b["id"]: b for b in context})
                        if accepted:
                            break
        findings.append({"id": identifier, **(accepted or {
            "score": None, "level": None, "reasons": [], "evidence_pages": [],
            "needs_review": True, "questions": [f"请核对 {identifier} 的评分条件和原文证据。"],
        })})
    ranked = [(item, confidence(item, rules["rubrics"][category][item["id"]]))
              for item in findings if item["score"] is not None]
    strengths = [summary_item(item) for item, position in sorted(ranked, key=lambda pair: -pair[1])
                 if position >= 0.67][:2]
    gaps = [summary_item(item) for item, position in sorted(ranked, key=lambda pair: pair[1])
            if position <= 0.33][:2]
    review = [item["id"] for item in findings if item["needs_review"]]
    sentences = []
    if strengths:
        sentences.append("亮点：" + "；".join(f"{item['criterion_id']} {item['text']}" for item in strengths))
    if gaps:
        sentences.append("主要不足：" + "；".join(f"{item['criterion_id']} {item['text']}" for item in gaps))
    if review:
        sentences.append("请人工复核 " + "、".join(review) + " 的评分条件和证据")
    text = "。".join(part.rstrip("。") for part in sentences) + "。" if sentences else "逐项评分及依据见六项初评结果。"
    return {"status": "completed_with_review_flags" if review else "completed",
            "criteria": findings, "summary": {"text": text, "strengths": strengths, "gaps": gaps,
                                              "review_points": review}}


def valid_conditions(items, rule, blocks):
    if not isinstance(items, list) or len(items) != len(rule["conditions"]):
        return False
    seen = set()
    for item in items:
        if not isinstance(item, dict) or type(item.get("index")) is not int:
            return False
        index = item["index"]
        status = item.get("status")
        ids = validated_ids(item.get("evidence_ids"), blocks)
        if not 0 <= index < len(items) or index in seen or status not in (
            "supported", "unsupported", "uncertain"
        ) or ids is None or (status == "supported" and not ids):
            return False
        seen.add(index)
    return True


def accepted_score(item, rule, blocks):
    value = item.get("score")
    if type(value) not in (int, float) or not 0 <= value <= 100 or not math.isfinite(value):
        return None
    band = next((level for level in rule["levels"] if level["id"] == item.get("level")), None)
    if not band or not band["min"] <= value <= band["max"]:
        return None
    reasons = item.get("reasons")
    if not isinstance(reasons, list) or not 1 <= len(reasons) <= 3:
        return None
    verified = []
    for reason in reasons:
        if not isinstance(reason, dict) or not isinstance(reason.get("text"), str):
            return None
        ids = validated_ids(reason.get("evidence_ids"), blocks)
        text = reason["text"].strip()
        if not ids or not 0 < len(text) <= 600:
            return None
        verified.append({"text": text, "evidence_ids": ids,
                         "evidence_pages": sorted({blocks[key]["page"] for key in ids})})
    return {"score": value, "level": band["id"], "reasons": verified,
            "evidence_pages": sorted({page for reason in verified for page in reason["evidence_pages"]}),
            "questions": [], "needs_review": False}


def confidence(item, rule):
    low = min(level["min"] for level in rule["levels"])
    high = max(level["max"] for level in rule["levels"])
    return (item["score"] - low) / (high - low) if high != low else 0.5


def summary_item(item):
    return {"criterion_id": item["id"], **item["reasons"][0]}
