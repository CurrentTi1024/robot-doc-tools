import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path

from app.config import CRITERIA


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def rate(numerator, denominator):
    return round(numerator / denominator, 4) if denominator else None


def compare(cases):
    by_id = defaultdict(list)
    checked = missing = detected = present = false_missing = uncertain = 0
    evidence_total = evidence_correct = 0
    for case in cases:
        expected = case["checklist_expected"]
        actual = {item["item_id"]: item for item in load(case["checklist_run"])["items"]}
        if set(expected) != set(actual):
            raise ValueError("Checklist output IDs differ from the Golden labels")
        for identifier, label in expected.items():
            item = actual[identifier]
            checked += 1
            if label["status"] == "missing":
                missing += 1
                detected += item["status"] == "missing"
            if label["status"] == "present":
                present += 1
                false_missing += item["status"] == "missing"
            uncertain += item["status"] == "uncertain"
            predicted = set(item["evidence_pages"])
            evidence_total += len(predicted)
            evidence_correct += len(predicted & set(label["evidence_pages"]))

        runs = [{item["id"]: item for item in load(path)["criteria"]}
                for path in case["scoring_runs"]]
        if not runs or any(set(run) != set(CRITERIA) for run in runs):
            raise ValueError("A scoring run needs all six criteria")
        for identifier in CRITERIA:
            first, second = case["human"][identifier]
            values = [run[identifier]["score"] for run in runs if run[identifier]["score"] is not None]
            errors = [abs(value - (first["score"] + second["score"]) / 2) for value in values]
            by_id[identifier].append({
                "errors": errors, "human_gap": abs(first["score"] - second["score"]),
                "agreed": sum(run[identifier]["level"] == first["level"] for run in runs)
                if first["level"] == second["level"] else 0,
                "comparable": len(runs) if first["level"] == second["level"] else 0,
                "review": len(runs) - len(values), "runs": len(runs),
                "stdev": statistics.pstdev(values) if len(values) > 1 else None,
            })
    scoring = {}
    for key, rows in {**by_id, "overall": [row for rows in by_id.values() for row in rows]}.items():
        errors = [abs_error for row in rows for abs_error in row["errors"]]
        deviations = [row["stdev"] for row in rows if row["stdev"] is not None]
        scoring[key] = {
            "mae": statistics.mean(errors) if errors else None,
            "human_mean_abs_gap": statistics.mean(row["human_gap"] for row in rows),
            "level_agreement_when_humans_agree": rate(sum(row["agreed"] for row in rows),
                                                       sum(row["comparable"] for row in rows)),
            "unscored_rate": rate(sum(row["review"] for row in rows), sum(row["runs"] for row in rows)),
            "mean_repeat_stdev": statistics.mean(deviations) if deviations else None,
        }
    return {"cases": len(cases), "checklist": {
        "items": checked, "missing_recall": rate(detected, missing),
        "false_missing_rate": rate(false_missing, present), "uncertain_rate": rate(uncertain, checked),
        "evidence_page_precision": rate(evidence_correct, evidence_total),
    }, "scoring": scoring}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    cases = load(args.manifest)
    if not cases:
        parser.error("Manifest needs at least one labeled proposal")
    print(json.dumps(compare(cases), ensure_ascii=False, indent=2))
