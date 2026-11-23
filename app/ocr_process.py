import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from .config import DATA, OCR_TIMEOUT, ROOT
from .document import extract
from .storage import Storage


def run_ocr(pdf_key, rules, prefix):
    DATA.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="ocr-job-", dir=DATA) as directory:
        request = Path(directory) / "request.json"
        result = Path(directory) / "result.json"
        request.write_text(json.dumps([pdf_key, rules, prefix]), encoding="utf-8")
        subprocess.run(
            [sys.executable, "-m", "app.ocr_process", str(request), str(result)],
            cwd=ROOT, timeout=OCR_TIMEOUT, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return json.loads(result.read_text(encoding="utf-8"))


if __name__ == "__main__":
    request, result = map(Path, sys.argv[1:])
    pdf_key, rules, prefix = json.loads(request.read_text(encoding="utf-8"))
    storage = Storage()
    evidence = extract(storage.get(pdf_key), rules, storage, prefix, temp_root=request.parent)
    result.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
