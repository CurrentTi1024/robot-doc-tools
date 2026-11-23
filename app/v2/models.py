import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from app.config import ROOT
from app.rules import load_rules


SKILLS = Path(__file__).resolve().parent / "skills"
DEMO_MODELS = ROOT / "configs" / "models-v2.json"


class ModelUnavailable(Exception):
    pass


class Settings:
    def __init__(self, model_file=None, rule_file=None):
        source = Path(rule_file or os.getenv("RW_V2_RULES", ROOT / "configs" / "sample.json"))
        self.rules, digest = load_rules(source)
        self.template = os.getenv("RW_V2_TEMPLATE")
        template_digest = hashlib.sha256(Path(self.template).read_bytes()).hexdigest() if self.template else ""
        digest_parts = [digest, template_digest]
        digest_parts.extend(hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in sorted(SKILLS.rglob("*.md")))
        self.rules_digest = hashlib.sha256("".join(digest_parts).encode()).hexdigest()
        config = json.loads(Path(model_file or os.getenv("RW_V2_MODELS", DEMO_MODELS)).read_text(encoding="utf-8"))
        self.profiles = config["models"]
        self.checklist = config["checklist"]
        self.judges = tuple(config["judges"])
        self.summary = config["summary"]
        self.soffice = os.getenv("RW_V2_SOFFICE", "soffice")
        if not 1 <= len(self.judges) <= 3 or len(set(self.judges)) != len(self.judges):
            raise ValueError("Configure one to three distinct judge profiles")
        for name in {self.checklist, self.summary, *self.judges}:
            if name not in self.profiles:
                raise ValueError(f"Unknown model profile: {name}")
        for profile in self.profiles.values():
            if profile.get("vision") and profile["vision"] not in self.profiles:
                raise ValueError("Unknown vision model profile")
        resolved = {name: {**profile, "model": os.getenv(profile["model_env"], ""),
                           "url": os.getenv(profile["url_env"], "")}
                    for name, profile in self.profiles.items()}
        self.model_digest = hashlib.sha256(json.dumps([config, resolved], sort_keys=True).encode()).hexdigest()

    def identity(self, name):
        profile = self.profiles[name]
        return os.getenv(profile["model_env"], "") or name

    def check_ready(self, name):
        profile = self.profiles[name]
        url = os.getenv(profile["url_env"], "")
        parsed = urlsplit(url)
        if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
            raise ModelUnavailable(f"Model profile {name} has an invalid endpoint")
        if not os.getenv(profile["model_env"]) or not os.getenv(profile["key_env"]):
            raise ModelUnavailable(f"Model profile {name} is not configured")

    def model(self, name):
        from langchain_openai import ChatOpenAI

        self.check_ready(name)
        profile = self.profiles[name]
        return ChatOpenAI(model=os.environ[profile["model_env"]],
                          base_url=os.environ[profile["url_env"]].rstrip("/"),
                          api_key=os.environ[profile["key_env"]],
                          timeout=45, max_retries=0)


def json_message(message):
    value = message.content
    if isinstance(value, list):
        value = "".join(part.get("text", "") for part in value
                        if isinstance(part, dict) and part.get("type") == "text")
    if not isinstance(value, str):
        raise ValueError("Expected JSON model output")
    value = value.strip()
    if value.startswith("```") and value.endswith("```"):
        value = value.split("\n", 1)[-1][:-3]
    obj = json.loads(value)
    if not isinstance(obj, dict):
        raise ValueError("Expected an object")
    return obj
