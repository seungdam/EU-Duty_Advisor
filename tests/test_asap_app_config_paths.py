from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _ProbeEntrypointPaths(
    *,
    environmentFilePath: Path,
    appConfigPath: Path | None,
) -> dict[str, str]:
    environment = os.environ.copy()
    environment["ASAP_PROJECT_ROOT"] = str(PROJECT_ROOT)
    environment["ASAP_ENV_FILE"] = str(environmentFilePath)
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(PROJECT_ROOT), environment.get("PYTHONPATH")))
    )
    if appConfigPath is None:
        environment.pop("ASAP_APP_CONFIG_PATH", None)
    else:
        environment["ASAP_APP_CONFIG_PATH"] = str(appConfigPath)

    probe = (
        "import json, asap_app; "
        "print('ASAP_PATHS=' + json.dumps({"
        "'config': str(asap_app.ASAP_APP_CONFIG_PATH), "
        "'environment': str(asap_app.ASAP_ENV_FILE_PATH)"
        "}))"
    )
    completedProcess = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=environmentFilePath.parent,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    marker = "ASAP_PATHS="
    payloadLine = next(
        line
        for line in reversed(completedProcess.stdout.splitlines())
        if line.startswith(marker)
    )
    return json.loads(payloadLine.removeprefix(marker))


def test_entrypoint_uses_default_and_injected_config_paths(tmp_path: Path) -> None:
    environmentFilePath = tmp_path / "missing.env"

    defaultPaths = _ProbeEntrypointPaths(
        environmentFilePath=environmentFilePath,
        appConfigPath=None,
    )
    assert defaultPaths == {
        "config": str(
            (PROJECT_ROOT / "config" / ".appconfig.asap_app.toml").resolve()
        ),
        "environment": str(environmentFilePath.resolve()),
    }

    injectedConfigPath = Path("config") / "injected.toml"
    injectedPaths = _ProbeEntrypointPaths(
        environmentFilePath=environmentFilePath,
        appConfigPath=injectedConfigPath,
    )
    assert injectedPaths["config"] == str(
        (PROJECT_ROOT / injectedConfigPath).resolve()
    )
