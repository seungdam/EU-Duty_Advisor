"""Single-thread execution boundary for shared OCR engines."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar


OcrResult = TypeVar("OcrResult")


class OcrExecutionCoordinator:
    """Run shared OCR engine operations on one process-local worker."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="shared-ocr",
        )

    def Execute(self, operation: Callable[[], OcrResult]) -> OcrResult:
        return self._executor.submit(operation).result()

    def Shutdown(self, *, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait)


SHARED_OCR_EXECUTION_COORDINATOR = OcrExecutionCoordinator()
