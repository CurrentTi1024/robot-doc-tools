import argparse
import hashlib
import json
import re
import subprocess
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

from app.config import DATA

HOST = "https://huggingface.co"
CHUNK_SIZE = 64 * 1024 * 1024
MODELS = {
    "layout": ("PaddlePaddle/PP-DocLayoutV3", {"inference.yml", "inference.json", "inference.pdiparams"}),
    "vl": ("PaddlePaddle/PaddleOCR-VL-1.6", {"inference.yml", "config.json", "model.safetensors"}),
}


def get_json(path):
    with urlopen(HOST + path, timeout=30) as response:
        return json.load(response)


def matches(path, item):
    if not path.is_file() or path.stat().st_size != item["size"]:
        return False
    lfs = item.get("lfs")
    if not lfs:
        return True
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() == lfs["oid"]


def download(url, target, size):
    curl = ["curl", "--fail", "--silent", "--show-error", "--location",
            "--retry", "2", "--connect-timeout", "20", "--max-time", "150"]
    if size <= 2 * CHUNK_SIZE:
        subprocess.run([*curl, "--output", str(target), url], check=True)
        return

    parts = target.with_suffix(target.suffix + ".chunks")
    parts.mkdir(exist_ok=True)

    def download_part(start):
        end = min(start + CHUNK_SIZE, size) - 1
        path = parts / f"{start}.bin"
        length = end - start + 1
        if not path.is_file() or path.stat().st_size != length:
            temporary = path.with_suffix(".part")
            response = subprocess.run(
                [*curl, "--range", f"{start}-{end}", "--max-filesize", str(length),
                 "--output", str(temporary), "--write-out", "%{http_code}",
                 f"{url}?download=true&rw_start={start}"], check=True, capture_output=True, text=True,
            )
            if response.stdout != "206" or temporary.stat().st_size != length:
                raise RuntimeError(f"Unexpected response for bytes {start}-{end}")
            temporary.replace(path)
            print(f"Received bytes {start:,}–{end:,}", flush=True)
        return path

    with ThreadPoolExecutor(max_workers=6) as pool:
        chunks = list(pool.map(download_part, range(0, size, CHUNK_SIZE)))
    with target.open("wb") as stream:
        for chunk in chunks:
            with chunk.open("rb") as source:
                shutil.copyfileobj(source, stream, 1024 * 1024)


def fetch(repo, revision, target, required):
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Expected a full model revision SHA")
    items = get_json(f"/api/models/{repo}/tree/{revision}")
    target.mkdir(parents=True, exist_ok=True)
    files = {}
    for item in items:
        name = item["path"]
        if item["type"] != "file" or not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9._-]*", name):
            continue
        path = target / name
        if not matches(path, item):
            temporary = path.with_suffix(path.suffix + ".download")
            print(f"Fetching {repo}/{name} ({item['size']:,} bytes)", flush=True)
            download(f"{HOST}/{repo}/resolve/{revision}/{quote(name)}", temporary, item["size"])
            if not matches(temporary, item):
                raise RuntimeError(f"Size or checksum mismatch in {repo}/{name}")
            temporary.replace(path)
            chunks = temporary.with_suffix(temporary.suffix + ".chunks")
            if chunks.is_dir():
                for part in chunks.glob("*.bin"):
                    part.unlink()
                chunks.rmdir()
        files[name] = {"size": item["size"], "sha256": item.get("lfs", {}).get("oid")}
    if not required <= files.keys():
        raise RuntimeError(f"Required files missing in {repo}")
    return {"repository": repo, "revision": revision, "files": files}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--layout-revision")
    parser.add_argument("--vl-revision")
    parser.add_argument("--output", type=Path, default=DATA / "models")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    if args.verify_only:
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8-sig"))
        for name, (repo, required) in MODELS.items():
            model = manifest[name]
            files = model["files"]
            if model["repository"] != repo or not required <= files.keys():
                raise RuntimeError(f"Wrong archive or incomplete manifest for {name}")
            for filename, metadata in files.items():
                if not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9._-]*", filename):
                    raise RuntimeError("Invalid model filename")
                checksum = metadata["sha256"]
                item = {"size": metadata["size"], "lfs": {"oid": checksum} if checksum else None}
                if not matches(output / name / filename, item):
                    raise RuntimeError(f"Offline verification failed for {name}/{filename}")
        print(f"Both model directories verified offline at {output}")
        return
    manifest = {
        name: fetch(repo, getattr(args, f"{name}_revision") or get_json(f"/api/models/{repo}")["sha"],
                    output / name, required)
        for name, (repo, required) in MODELS.items()
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Both model directories ready at {output}")


if __name__ == "__main__":
    main()
