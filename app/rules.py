import hashlib
import json

from .config import CRITERIA, RULES
from .document import normalized


def load_rules(path=RULES):
    data = path.read_bytes()
    rules = json.loads(data)
    if not isinstance(rules["version"], str) or not rules["version"].strip():
        raise ValueError("Provide a nonempty rules version")
    if rules["cross_page"] not in {"allow", "require_expected_page", "review"}:
        raise ValueError("Invalid cross-page policy")
    pages = [item["page"] for item in rules["pages"]]
    if sorted(pages) != list(range(1, 8)):
        raise ValueError("Provide seven template pages numbered 1 through 7")
    anchors = []
    for item in rules["pages"]:
        if not item["anchors"]:
            raise ValueError("Every template page needs anchors")
        anchors.extend(normalized(anchor) for anchor in item["anchors"])
    if "" in anchors or len(set(anchors)) != len(anchors):
        raise ValueError("Template anchors must be nonempty and distinct")
    seen = set()
    covered = set()
    for item in rules["checklist"]:
        if not item["id"].strip() or item["id"] in seen or item["page"] not in pages:
            raise ValueError("Invalid or duplicate Checklist item")
        if item["kind"] not in {"literal", "semantic"}:
            raise ValueError("Invalid Checklist rule kind")
        if not item["title"].strip() or not item["requirement"].strip():
            raise ValueError("Checklist needs a title and requirement")
        if item["kind"] == "literal" and not any(normalized(t) for t in item.get("terms", [])):
            raise ValueError("Literal rules require terms")
        seen.add(item["id"])
        covered.add(item["page"])
    if covered != set(pages):
        raise ValueError("Every template page must have a Checklist rule")
    if set(rules["rubrics"]) != {"A", "B", "C"}:
        raise ValueError("Provide A/B/C rubrics")
    for rubric in rules["rubrics"].values():
        if set(rubric) != set(CRITERIA):
            raise ValueError("Each rubric must have exactly A1 through B3")
        for item in rubric.values():
            if not item["name"].strip() or not item["conditions"] or not item["levels"]:
                raise ValueError("Provide criterion name, conditions and levels")
            previous = -1
            names = set()
            for level in sorted(item["levels"], key=lambda level: level["min"]):
                low, high = level["min"], level["max"]
                if not 0 <= low <= high <= 100 or low <= previous or level["id"] in names:
                    raise ValueError("Invalid or overlapping rating bands")
                if not level["id"].strip() or not level["description"].strip():
                    raise ValueError("Provide level ID and description")
                previous = high
                names.add(level["id"])
    return rules, hashlib.sha256(data).hexdigest()
