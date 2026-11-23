import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from .config import ROOT


class ModelUnavailable(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def settings(*, allow_local_file=True):
    url, model, key = (os.getenv(f"RW_LLM_{name}") for name in ("URL", "MODEL", "KEY"))
    configured = (url, model, key)
    if any(configured) and not all(configured):
        raise ModelUnavailable("Explicit LLM configuration is incomplete")
    if not any(configured) and allow_local_file:
        explicit = os.getenv("RW_LLM_FILE")
        candidates = [Path(explicit)] if explicit else [
            ROOT.parent / "deepseek.txt", ROOT.parent / "deeepseek.txt"
        ]
        path = next((item for item in candidates if item.is_file()), None)
        if path is not None:
            content = path.read_text(encoding="utf-8-sig")
            urls = re.findall(r"https://[^\s\"'<>，。]+", content)
            bases = [item for item in urls if urlparse(item).path.rstrip("/") in ("", "/v1", "/beta")
                     or urlparse(item).path.rstrip("/").endswith("/chat/completions")]
            models = re.findall(r"(?<![./\w-])deepseek-[a-zA-Z0-9_.-]+", content)
            keys = re.findall(r"(?<![\w-])sk-[a-zA-Z0-9_-]+", content)
            url = url or (bases[-1] if bases else (urls[-1] if urls else None))
            model = model or (models[-1] if models else None)
            key = key or (keys[-1] if keys else None)
    if not all((url, model, key)):
        raise ModelUnavailable("LLM configuration incomplete")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ModelUnavailable("Invalid LLM endpoint")
    url = url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    return url, model, key


class DeepSeek:
    def __init__(self, *, allow_local_file=True):
        self.url, self.model, self.key = settings(allow_local_file=allow_local_file)

    def complete(self, messages):
        request = urllib.request.Request(
            self.url,
            data=json.dumps({
                "model": self.model,
                "temperature": 0,
                "max_tokens": 1800,
                "response_format": {"type": "json_object"},
                "messages": messages,
            }, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
        )
        try:
            opener = urllib.request.build_opener(NoRedirect)
            with opener.open(request, timeout=35) as response:
                body = json.loads(response.read(2_000_000))
            result = json.loads(body["choices"][0]["message"]["content"])
            if not isinstance(result, dict):
                raise ValueError("Expected JSON object")
            return result
        except urllib.error.HTTPError as exc:
            raise ModelUnavailable(f"LLM returned HTTP {exc.code}") from None
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            raise ModelUnavailable("LLM response unavailable or invalid") from None
