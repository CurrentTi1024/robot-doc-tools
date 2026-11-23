import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("RW_DATA", ROOT / ".local")).resolve()
DATABASE = os.environ.get("RW_DATABASE", str(DATA / "service.db"))
BROKER = os.environ.get("RW_BROKER", "redis://127.0.0.1:6379/0")
MAX_BYTES = int(os.environ.get("RW_MAX_BYTES", 30 * 1024 * 1024))
CRITERIA = ("A1", "A2", "A3", "B1", "B2", "B3")
