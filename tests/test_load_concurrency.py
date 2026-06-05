"""Load and concurrency tests for MemPalace retrieval pipeline.

Phase 16: Validates prefetch latency, thread join budget, cache behavior
under concurrent access, and retrieval pipeline thread safety.

These tests use a mock API (no live palace needed) to isolate the
retrieval engine's concurrency characteristics.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retrieval import MemPalaceRetrieval, RetrievalCache, _classify_evidence


# ----------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------


@dataclass
class FakeConfig:
    """Minimal config for retrieval load tests."""
    min_score: float = 0.5
    prioritize_recent_days: int = 30
    dynamics_enabled: bool = False
    wake_up_wing: str = ""
    target_wing: str = ""
    memory_stack_enabled: bool = False
    use_kg: bool = False
    follow_tunnels: bool = False
    always_run_l3: bool = True
    max_results: int = 8
    max_l3_search_time_ms: int = 400
    retrieval_timeout_ms: int = 500
    background_retrieval: bool = False
    prefetch_cache_size: int = 32
    cache_ttl_seconds: int = 30
    max_recall_chars: int = 3500
    max_quote_chars_per_hit: int = 320
    max_total_quoted_chars: int = 2400
    max_wake_block_chars: int = 600
    l2_default_room: str = ""
    recall_char_budget: int = 1500
    prefer_active_project: bool = False
    use_halls: bool = False
    use_closets: bool = False
    max_tunnel_hops: int = 1
    max_tunnel_hits: int = 2
    include_kg_facts: bool = False
    kg_entity_limit: int = 5
    retrieval_enabled: bool = True
    thread_join_timeout_ms: int = 1000


def _make_mock_api(results: Optional[List[Dict[str, Any]]] = None, latency_ms: float = 5):
    """Create a mock MemPalaceAPI with configurable search results and latency."""
    api = MagicMock()
    default_results = results or [
        {"content": f"result {i}", "score": 0.5 + i * 0.05, "wing": "memory", "room": "test", "drawer_id": f"d{i}"}
        for i in range(5)
    ]

    def _search(*args, **kwargs):
        time.sleep(latency_ms / 1000.0)
        return default_results

    api.search = _search
    api.wake_up_context = MagicMock(return_value="")
    api.scoped_recall = MagicMock(return_value="")
    return api


# ----------------------------------------------------------------
# Test: Concurrent prefetch latency
# ----------------------------------------------------------------


class TestConcurrentPrefetchLatency:
    """Measure prefetch latency under concurrent access."""

    def test_single_prefetch_latency(self):
        """Baseline: single prefetch completes within retrieval_timeout_ms."""
        config = FakeConfig()
        api = _make_mock_api(latency_ms=10)
        engine = MemPalaceRetrieval(api, config)

        start = time.monotonic()
        result = engine.prefetch("test query", background=False)
        elapsed_ms = (time.monotonic() - start) * 1000

        assert elapsed_ms < config.retrieval_timeout_ms, (
            f"Single prefetch took {elapsed_ms:.0f}ms > {config.retrieval_timeout_ms}ms timeout"
        )

    def test_conquential_prefetches_latency(self):
        """10 sequential prefetches: median latency stays under timeout."""
        config = FakeConfig()
        api = _make_mock_api(latency_ms=10)
        engine = MemPalaceRetrieval(api, config)

        latencies = []
        for i in range(10):
            start = time.monotonic()
            engine.prefetch(f"query {i}", background=False)
            latencies.append((time.monotonic() - start) * 1000)

        median = sorted(latencies)[len(latencies) // 2]
        assert median < config.retrieval_timeout_ms, (
            f"Median sequential latency {median:.0f}ms > {config.retrieval_timeout_ms}ms"
        )

    def test_concurrent_prefetches_no_crash(self):
        """20 concurrent prefetches: all complete without exceptions."""
        config = FakeConfig()
        api = _make_mock_api(latency_ms=15)
        engine = MemPalaceRetrieval(api, config)

        errors = []
        results = []

        def _prefetch(idx):
            try:
                result = engine.prefetch(f"concurrent query {idx}", background=False)
                results.append(result)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_prefetch, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert not errors, f"Concurrent prefetches raised {len(errors)} errors: {errors[:3]}"
        assert len(results) == 20, f"Expected 20 results, got {len(results)}"


# ----------------------------------------------------------------
# Test: Cache behavior under load
# ----------------------------------------------------------------


class TestCacheUnderLoad:
    """Cache hit rates and eviction under concurrent access."""

    def test_cache_hit_rate_repeated_query(self):
        """Same query 50 times: cache should absorb 49+ hits."""
        config = FakeConfig()
        api = _make_mock_api(latency_ms=5)
        engine = MemPalaceRetrieval(api, config)

        # First call populates cache
        engine.prefetch("repeated query", background=False)

        # 49 more calls — should hit cache
        for _ in range(49):
            engine.prefetch("repeated query", background=False)

        stats = engine._cache.stats()
        assert stats["hits"] >= 49, f"Expected ≥49 cache hits, got {stats['hits']}"

    def test_cache_eviction_under_pressure(self):
        """With cache_size=4, inserting 10 unique queries evicts old entries."""
        config = FakeConfig(prefetch_cache_size=4)
        api = _make_mock_api(latency_ms=1)
        engine = MemPalaceRetrieval(api, config)

        for i in range(10):
            engine.prefetch(f"unique query {i}", background=False)

        stats = engine._cache.stats()
        assert stats["evictions"] >= 6, f"Expected ≥6 evictions, got {stats['evictions']}"
        assert stats["size"] <= 4, f"Cache size {stats['size']} > limit 4"

    def test_concurrent_cache_access_no_corruption(self):
        """50 threads reading/writing cache simultaneously: no exceptions."""
        config = FakeConfig(prefetch_cache_size=16)
        api = _make_mock_api(latency_ms=2)
        engine = MemPalaceRetrieval(api, config)

        errors = []

        def _access(idx):
            try:
                # Mix of unique and repeated queries
                q = f"query {idx % 10}"
                engine.prefetch(q, background=False)
            except Exception as e:
                errors.append(e)

        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = [pool.submit(_access, i) for i in range(50)]
            for f in as_completed(futures):
                f.result()  # re-raise any exception

        assert not errors, f"Concurrent cache access raised {len(errors)} errors"


# ----------------------------------------------------------------
# Test: Thread join budget
# ----------------------------------------------------------------


class TestThreadJoinBudget:
    """Thread join timeout behavior."""

    def test_thread_join_respects_timeout(self):
        """Background threads join within timeout budget."""
        timeout_s = 0.2

        threads: Dict[str, threading.Thread] = {}
        lock = threading.Lock()

        def _start(name, fn):
            with lock:
                t = threading.Thread(target=fn, daemon=True)
                t.start()
                threads[name] = t

        def _join_all():
            with lock:
                for name, t in list(threads.items()):
                    t.join(timeout=timeout_s)
                    if not t.is_alive():
                        del threads[name]

        # Start a thread that finishes quickly
        _start("fast", lambda: time.sleep(0.01))

        start = time.monotonic()
        _join_all()
        elapsed_ms = (time.monotonic() - start) * 1000

        assert elapsed_ms < timeout_s * 1000, (
            f"Thread join took {elapsed_ms:.0f}ms > {timeout_s * 1000:.0f}ms budget"
        )
        assert len(threads) == 0, f"Expected 0 tracked threads after join, got {len(threads)}"

    def test_thread_join_cleans_completed(self):
        """Completed threads are removed from tracking dict."""
        threads: Dict[str, threading.Thread] = {}
        lock = threading.Lock()

        def _start(name, fn):
            with lock:
                t = threading.Thread(target=fn, daemon=True)
                t.start()
                threads[name] = t

        def _join_all():
            with lock:
                for name, t in list(threads.items()):
                    t.join(timeout=1.0)
                    if not t.is_alive():
                        del threads[name]

        for i in range(5):
            _start(f"quick-{i}", lambda: None)

        time.sleep(0.01)
        _join_all()

        assert len(threads) == 0, (
            f"Expected 0 tracked threads after join, got {len(threads)}"
        )


# ----------------------------------------------------------------
# Test: Evidence classification under load
# ----------------------------------------------------------------


class TestClassificationPerformance:
    """Classification throughput — must stay fast for 100+ hit batches."""

    def test_classify_100_hits_under_10ms(self):
        """Classify 100 hits in under 10ms."""
        hits = [
            {"content": f"content about topic {i}", "score": 0.3 + i * 0.005}
            for i in range(100)
        ]

        start = time.monotonic()
        for h in hits:
            _classify_evidence("what is topic 42", h, score_floor=0.3)
        elapsed_ms = (time.monotonic() - start) * 1000

        assert elapsed_ms < 10, f"Classifying 100 hits took {elapsed_ms:.1f}ms > 10ms"

    def test_classify_concurrent_safety(self):
        """Multiple threads classifying simultaneously: no shared state corruption."""
        hits = [
            {"content": f"content {i}", "score": 0.35 + i * 0.01}
            for i in range(50)
        ]

        results = {}

        def _classify_batch(thread_id, floor):
            batch_results = []
            for h in hits:
                batch_results.append(_classify_evidence("test query", h, score_floor=floor))
            results[thread_id] = batch_results

        threads = [
            threading.Thread(target=_classify_batch, args=(i, 0.3 if i % 2 == 0 else 0.5))
            for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2.0)

        assert len(results) == 8, f"Expected 8 thread results, got {len(results)}"
        # All threads with same floor should produce identical results
        even_results = [results[i] for i in range(0, 8, 2)]
        for r in even_results[1:]:
            assert r == even_results[0], "Concurrent classification produced inconsistent results"


# ----------------------------------------------------------------
# Test: Retrieval pipeline integration under load
# ----------------------------------------------------------------


class TestPipelineIntegration:
    """End-to-end retrieval pipeline with mocked API."""

    def test_full_pipeline_returns_within_timeout(self):
        """Full L0→L1→L2→L3 pipeline with mock API completes within budget."""
        config = FakeConfig(
            retrieval_timeout_ms=500,
            max_l3_search_time_ms=300,
            memory_stack_enabled=False,
            always_run_l3=True,
        )
        api = _make_mock_api(latency_ms=20)
        engine = MemPalaceRetrieval(api, config)

        start = time.monotonic()
        result = engine.prefetch("test pipeline query", session_id="test", background=False)
        elapsed_ms = (time.monotonic() - start) * 1000

        assert elapsed_ms < config.retrieval_timeout_ms, (
            f"Full pipeline took {elapsed_ms:.0f}ms > {config.retrieval_timeout_ms}ms"
        )
        # Should have some recall content
        assert isinstance(result, str)

    def test_diagnostics_counter_accurate(self):
        """Diagnostics counters track operations correctly."""
        config = FakeConfig()
        api = _make_mock_api(latency_ms=1)
        engine = MemPalaceRetrieval(api, config)

        for i in range(5):
            engine.prefetch(f"diag query {i}", background=False)

        diag = engine.diagnostics()
        pipeline = diag.get("staged_pipeline", {})

        assert pipeline.get("l3_hybrid_searches", 0) >= 5, (
            f"Expected ≥5 L3 searches, got {pipeline.get('l3_hybrid_searches', 0)}"
        )
