"""JSON artifact file helpers."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import sleep


def WriteJsonAtomically(targetPath: Path, payload: object) -> None:
    """Publish complete JSON without exposing a partially written target."""

    targetPath.parent.mkdir(parents=True, exist_ok=True)
    temporaryPath: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=targetPath.parent,
            prefix=f".{targetPath.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporaryFile:
            temporaryPath = Path(temporaryFile.name)
            json.dump(payload, temporaryFile, ensure_ascii=False, indent=2)
        for attempt in range(20):
            try:
                temporaryPath.replace(targetPath)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                # Windows reader가 잠시 target 교체를 막을 수 있다.
                sleep(0.01)
    finally:
        if temporaryPath is not None:
            temporaryPath.unlink(missing_ok=True)
