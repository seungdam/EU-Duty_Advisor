# tests/conftest.py

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"

for path in (PROJECT_ROOT, SRC_ROOT):
    value = str(path)
    if value not in sys.path:
        sys.path.insert(0, value)