from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from bussiness_logic.product.web_parser.kurly_market_collector import (
    KurlyPageCollector,
)
from bussiness_logic.product.web_parser.kurly_market_schema import (
    RenderedPageEvidence,
)


class _SupportedParser:
    def IsSupportedProductPageUrl(self, _url: str) -> bool:
        return True


class _CollectionState:
    def __init__(self, barrier: threading.Barrier | None = None) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.maximum = 0
        self.enteredCount = 0
        self.firstEntered = threading.Event()
        self.secondEntered = threading.Event()
        self.release = threading.Event()
        self.barrier = barrier

    def Enter(self) -> None:
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            self.enteredCount += 1
            if self.enteredCount == 1:
                self.firstEntered.set()
            elif self.enteredCount == 2:
                self.secondEntered.set()

    def Exit(self) -> None:
        with self.lock:
            self.active -= 1


class _BlockingCollector(KurlyPageCollector):
    def __init__(
        self,
        gate: threading.BoundedSemaphore,
        state: _CollectionState,
    ) -> None:
        super().__init__(
            parser=_SupportedParser(),
            concurrencyGate=gate,
        )
        self._state = state

    def _CollectRenderedPageEvidence(
        self,
        productPageUrl: str,
    ) -> RenderedPageEvidence:
        self._state.Enter()
        try:
            if self._state.barrier is not None:
                self._state.barrier.wait(timeout=3.0)
            else:
                assert self._state.release.wait(timeout=3.0)
            return RenderedPageEvidence(productPageUrl=productPageUrl)
        finally:
            self._state.Exit()


def test_crawler_gate_serializes_collectors_at_concurrency_one() -> None:
    gate = threading.BoundedSemaphore(1)
    state = _CollectionState()
    collectors = [
        _BlockingCollector(gate, state),
        _BlockingCollector(gate, state),
    ]
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            firstFuture = executor.submit(
                collectors[0].CollectRenderedPageEvidence,
                "https://example.com/first",
            )
            assert state.firstEntered.wait(timeout=3.0)
            secondFuture = executor.submit(
                collectors[1].CollectRenderedPageEvidence,
                "https://example.com/second",
            )
            assert not state.secondEntered.wait(timeout=0.1)
            state.release.set()
            firstFuture.result(timeout=3.0)
            secondFuture.result(timeout=3.0)
    finally:
        state.release.set()
    assert state.maximum == 1


def test_crawler_gate_allows_configured_parallel_collections() -> None:
    gate = threading.BoundedSemaphore(2)
    state = _CollectionState(threading.Barrier(2))
    collectors = [
        _BlockingCollector(gate, state),
        _BlockingCollector(gate, state),
    ]
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                collector.CollectRenderedPageEvidence,
                f"https://example.com/{index}",
            )
            for index, collector in enumerate(collectors)
        ]
        for future in futures:
            future.result(timeout=3.0)
    assert state.maximum == 2
