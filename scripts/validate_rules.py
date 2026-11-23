import argparse
from pathlib import Path

from app.rules import load_rules


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    rules, _ = load_rules(args.path)
    print(f"Valid rules: {rules['version']}, {len(rules['checklist'])} Checklist items, 6 criteria x 3 categories")
