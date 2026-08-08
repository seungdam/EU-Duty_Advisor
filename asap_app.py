"""ASAP pipeline backend entrypoint."""

from __future__ import annotations

import atexit
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import torch


ASAP_ROOT = Path(
    os.environ.get("ASAP_PROJECT_ROOT", Path(__file__).resolve().parent),
).resolve()


def _ResolveProjectPath(configuredPath: str | None, defaultPath: Path) -> Path:
    resolvedPath = (
        Path(configuredPath).expanduser() if configuredPath else defaultPath
    )
    if not resolvedPath.is_absolute():
        resolvedPath = ASAP_ROOT / resolvedPath
    return resolvedPath.resolve()


ASAP_APP_CONFIG_PATH = _ResolveProjectPath(
    os.environ.get("ASAP_APP_CONFIG_PATH"),
    ASAP_ROOT / "config" / ".appconfig.asap_app.toml",
)
ASAP_ENV_FILE_PATH = _ResolveProjectPath(
    os.environ.get("ASAP_ENV_FILE"),
    ASAP_ROOT / ".env.asap_app",
)
os.environ["ASAP_APP_CONFIG_PATH"] = str(ASAP_APP_CONFIG_PATH)
os.environ["ASAP_ENV_FILE"] = str(ASAP_ENV_FILE_PATH)

import sys
ASAP_SRC_ROOT = ASAP_ROOT / "src"
for searchPath in (ASAP_ROOT, ASAP_SRC_ROOT):
    if searchPath.exists() and str(searchPath) not in sys.path:
        sys.path.insert(0, str(searchPath))

from bussiness_logic.app_config import LoadAppConfig, LoadEnvironmentFile

LoadEnvironmentFile(ASAP_ENV_FILE_PATH)

from backend.app import CreateBackendApp


appConfig = LoadAppConfig(ASAP_ROOT)
app = CreateBackendApp(
    pipelineRunMaxWorkers=appConfig.pipeline_execution.max_workers,
    pipelineRunMaxQueuedRuns=appConfig.pipeline_execution.max_queued_runs,
    allowedFrontendOrigins=appConfig.web.allowed_frontend_origins,
    webappDistDir=ASAP_ROOT / "webapp" / "dist",
)
server = app


def _CanConnect(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _WaitForPort(host: str, port: int, timeoutSeconds: float = 60.0) -> bool:
    deadline = time.monotonic() + timeoutSeconds
    while time.monotonic() < deadline:
        if _CanConnect(host, port):
            return True
        time.sleep(0.5)
    return False


def _StartMlxVlmServerIfNeeded() -> subprocess.Popen[bytes] | None:
    smokeConfig = appConfig.kurly_smoke
    if (
        not smokeConfig.use_structured_ocr
        or smokeConfig.structured_ocr_provider != "paddleocr_vl"
        or smokeConfig.structured_ocr_vl_rec_backend != "mlx-vlm-server"
    ):
        return None

    serverUrl = smokeConfig.structured_ocr_vl_rec_server_url or "http://localhost:8111/"
    parsedUrl = urlparse(serverUrl)
    host = parsedUrl.hostname or "localhost"
    if host not in {"localhost", "127.0.0.1", "::1"}:
        print("MLX-VLM server auto-start skipped: non-local server url")
        return None
    port = parsedUrl.port or 8111
    if _CanConnect(host, port):
        print("MLX-VLM server already running: {0}:{1}".format(host, port))
        return None

    executable = shutil.which("mlx_vlm.server")
    if executable is None:
        print("MLX-VLM server auto-start skipped: mlx_vlm.server not found")
        return None

    process = subprocess.Popen([executable, "--port", str(port)])
    if _WaitForPort(host, port):
        print("MLX-VLM server started: {0}:{1}".format(host, port))
    elif process.poll() is None:
        print("MLX-VLM server starting in background: {0}:{1}".format(host, port))
    else:
        print("MLX-VLM server failed: exit_code={0}".format(process.returncode))
        return None
    return process


def _RegisterProcessCleanup(process: subprocess.Popen[bytes]) -> None:
    def cleanup() -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    atexit.register(cleanup)


if __name__ == "__main__":
    mlxVlmProcess = _StartMlxVlmServerIfNeeded()
    if mlxVlmProcess is not None:
        _RegisterProcessCleanup(mlxVlmProcess)
    app.run(
        debug=False,
        host=appConfig.web.backend_host,
        port=appConfig.web.backend_port,
        threaded=True,
    )
