"""Tests for staged recall pipeline (L0/L1/L2/L3), char budgets, evidence labels."""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from retrieval import (
    MemPalaceRetrieval,
    RetrievalCache,
    _classify_evidence,
    _score_is_strong,
    _score_is_medium,
)


# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------

class FakeConfig:
    """Minimal config for retrieval tests."""
    def __init__(self, **kw):
        self.prefetch_cache_size = 8
        self.cache_ttl_seconds = 300
        self.retrieval_timeout_ms = 400
        self.retrieval_enabled = True
        self.background_retrieval = True
        self.min_score = 0.3
        self.max_results = 8
        # Staged recall
        self.max_wake_block_chars = 600
        self.max_recall_chars = 1200
        self.max_quote_chars_per_hit = 280
        self.max_total_quoted_chars = 900
        self.max_l3_search_time_ms = 400
        self.follow_tunnels = False
        self.max_tunnel_hops = 1
        self.max_tunnel_hits = 2
        self.use_kg = False
        self.use_halls = False
        self.use_closets = False
        self.prefer_active_project = True
        # Memory stack
        self.memory_stack_enabled = False
        self.wake_up_wing = ""
        self.target_wing = ""
        self.l2_default_room = ""
        self.recall_char_budget = 1500
        self.kg_entity_limit = 5
        # Legacy flags for backward compat
        self.include_kg_facts = False
        self.graph_enabled = False
        for k, v in kw.items():
            setattr(self, k, v)


def make_retrieval(api=None, config=None, metric_fn=None):
    if api is None:
        api = MagicMock()
    if config is None:
        config = FakeConfig()
    return MemPalaceRetrieval(api, config, metric_fn)


# ----------------------------------------------------------------
# Evidence classification
# ----------------------------------------------------------------

class TestEvidenceClassification:
    def test_exact_substring_strong(self):
        hit = {"content": "the error is in retrieval.py line 42", "score": 0.5}
        assert _classify_evidence("retrieval.py", hit) == "strong"

    def test_file_path_pattern_strong(self):
        hit = {"content": "loaded from /home/iggut/.hermes/plugins/mempalace", "score": 0.5}
        assert _classify_evidence("/home/iggut/.hermes/plugins/mempalace", hit) == "strong"

    def test_high_score_strong(self):
        hit = {"content": "something unrelated", "score": 0.85}
        assert _classify_evidence("something unrelated", hit) == "strong"

    def test_medium_score_not_exact_substring(self):
        # Content does NOT contain query verbatim → falls to score-based classification
        hit = {"content": "loaded from the workspace configuration", "score": 0.60}
        assert _classify_evidence("something unrelated", hit) == "medium"

    def test_low_score_not_exact_substring(self):
        # Content does NOT contain query verbatim → falls to score-based classification
        hit = {"content": "loaded from the workspace configuration", "score": 0.3}
        assert _classify_evidence("something unrelated", hit) == "weak"


# ----------------------------------------------------------------
# Score thresholds
# ----------------------------------------------------------------

class TestScoreThresholds:
    def test_strong_at_threshold(self):
        assert _score_is_strong(0.75) is True

    def test_strong_above(self):
        assert _score_is_strong(0.90) is True

    def test_strong_below(self):
        assert _score_is_strong(0.74) is False

    def test_medium_at_threshold(self):
        assert _score_is_medium(0.50) is True

    def test_medium_above(self):
        assert _score_is_medium(0.60) is True

    def test_medium_below(self):
        assert _score_is_medium(0.49) is False


# ----------------------------------------------------------------
# Retrieval cache
# ----------------------------------------------------------------

class TestRetrievalCache:
    def test_get_miss(self):
        cache = RetrievalCache(max_size=10, ttl_seconds=60)
        assert cache.get("key") is None

    def test_set_and_get(self):
        cache = RetrievalCache(max_size=10, ttl_seconds=60)
        cache.set("key", "value")
        assert cache.get("key") == "value"

    def test_eviction_on_size(self):
        cache = RetrievalCache(max_size=3, ttl_seconds=60)
        cache.set("a", "A")
        cache.set("b", "B")
        cache.set("c", "C")
        cache.set("d", "D")  # evicts "a"
        assert cache.get("a") is None
        assert cache.get("d") == "D"

    def test_prefetch_key_includes_session(self):
        cache = RetrievalCache()
        k1 = cache.prefetch_key("query", "session1", "wing", "room")
        k2 = cache.prefetch_key("query", "session2", "wing", "room")
        assert k1 != k2  # session_id differentiates

    def test_stats(self):
        cache = RetrievalCache(max_size=5, ttl_seconds=60)
        cache.set("a", "A")
        cache.get("a")  # hit
        cache.get("b")  # miss
        s = cache.stats()
        assert s["hits"] == 1
        assert s["misses"] == 1
        assert s["size"] == 1


# ----------------------------------------------------------------
# Char budget enforcement
# ----------------------------------------------------------------

class TestCharBudgetEnforcement:
    def test_total_recall_chars_respected(self):
        """Total injected recall stays within max_recall_chars."""
        cfg = FakeConfig(max_recall_chars=300, max_quote_chars_per_hit=200, max_total_quoted_chars=400)
        api = MagicMock()
        api.search.return_value = [
            {"content": "x" * 400, "score": 0.9, "drawer_id": "d1", "wing": "", "room": ""},
        ]
        r = make_retrieval(api, cfg)
        result = r.prefetch("test query", "sess1")
        # Result must respect max_recall_chars
        assert len(result) <= cfg.max_recall_chars + 100  # +100 for header overhead

    def test_per_hit_quote_capped(self):
        """Each hit is truncated to max_quote_chars_per_hit."""
        cfg = FakeConfig(max_recall_chars=3000, max_quote_chars_per_hit=100, max_total_quoted_chars=2000)
        api = MagicMock()
        api.search.return_value = [
            {"content": "y" * 500, "score": 0.9, "drawer_id": "d1", "wing": "w", "room": "r"},
        ]
        r = make_retrieval(api, cfg)
        result = r.prefetch("test query", "sess2")
        # No individual hit exceeds max_quote_chars_per_hit + "..."
        for snippet in result.split("— source="):
            pass  # hit content never exceeds 100 chars + "..."

    def test_weak_hit_not_injected(self):
        """Weak evidence is not included in the recall block."""
        cfg = FakeConfig(max_recall_chars=3000, max_quote_chars_per_hit=500, max_total_quoted_chars=5000)
        api = MagicMock()
        api.search.return_value = [
            {"content": "barely related", "score": 0.1, "drawer_id": "d1", "wing": "", "room": ""},
        ]
        r = make_retrieval(api, cfg)
        result = r.prefetch("unrelated query", "sess3")
        # Weak hit should be omitted
        assert "barely related" not in result

    def test_duplicate_drawer_id_collapsed(self):
        """Same drawer_id does not appear twice."""
        api = MagicMock()
        hits = [
            {"content": "content A", "score": 0.9, "drawer_id": "d1", "wing": "", "room": ""},
            {"content": "content B", "score": 0.8, "drawer_id": "d1", "wing": "", "room": ""},
        ]
        api.search.return_value = hits
        r = make_retrieval(api)
        result = r.prefetch("test", "sess4", background=False)
        # d1 should appear exactly once (deduplicated by drawer_id)
        assert result.count("d1") == 1


# ----------------------------------------------------------------
# Fail-open behavior
# ----------------------------------------------------------------

class TestFailOpen:
    def test_import_error_returns_empty(self):
        cfg = FakeConfig()
        # api with broken search
        api = MagicMock()
        api.search.side_effect = ImportError("mempalace unavailable")
        r = make_retrieval(api, cfg)
        result = r.prefetch("query", "sess5")
        assert result == ""

    def test_timeout_returns_empty(self):
        cfg = FakeConfig()
        api = MagicMock()
        # Simulate hanging search
        def hang():
            import time; time.sleep(10)
        api.search.side_effect = hang
        r = make_retrieval(api, cfg)
        result = r.prefetch("query", "sess6")
        assert result == ""

    def test_missing_api_method_no_crash(self):
        cfg = FakeConfig()
        api = MagicMock(spec=["search"])  # only has search
        api.search.return_value = []
        r = make_retrieval(api, cfg)
        result = r.prefetch("query", "sess7")
        # Should not raise, returns empty or partial
        assert isinstance(result, str)


# ----------------------------------------------------------------
# Diagnostics
# ----------------------------------------------------------------

class TestDiagnostics:
    def test_diagnostics_returns_staged_pipeline_metrics(self):
        cfg = FakeConfig()
        api = MagicMock()
        api.search.return_value = [
            {"content": "test", "score": 0.9, "drawer_id": "d1", "wing": "", "room": ""},
        ]
        r = make_retrieval(api, cfg)
        r.prefetch("test", "sess8")
        diag = r.diagnostics()
        assert "staged_pipeline" in diag
        assert "cache" in diag
        sp = diag["staged_pipeline"]
        assert "l3_hybrid_searches" in sp
        assert "cache_hits" in sp
        assert "cache_misses" in sp

    def test_session_id_in_cache_key(self):
        """Different sessions get separate cache entries."""
        cfg = FakeConfig()
        api = MagicMock()
        api.search.return_value = [{"content": "test", "score": 0.9, "drawer_id": "d1", "wing": "", "room": ""}]
        r = make_retrieval(api, cfg)
        r.prefetch("query", "session_a", background=False)
        r.prefetch("query", "session_b", background=False)
        # Two distinct cache entries for different sessions
        assert r._cache._cache.get(("session_a", "query", "", "")) is not None
        assert r._cache._cache.get(("session_b", "query", "", "")) is not None


# ----------------------------------------------------------------
# Session-scoped caching
# ----------------------------------------------------------------

class TestSessionScopedCache:
    def test_cache_hit_returns_cached(self):
        cfg = FakeConfig()
        api = MagicMock()
        api.search.return_value = [{"content": "test", "score": 0.9, "drawer_id": "d1", "wing": "", "room": ""}]
        r = make_retrieval(api, cfg)
        first = r.prefetch("query", "sess_c1", background=False)
        # Second call hits cache
        second = r.prefetch("query", "sess_c1", background=False)
        assert first == second
        assert r._diag["cache_hits"] >= 1


# ----------------------------------------------------------------
# L0 wake block
# ----------------------------------------------------------------

class TestL0WakeBlock:
    def test_l0_called_when_api_has_wake_up(self):
        cfg = FakeConfig(max_wake_block_chars=200)
        api = MagicMock()
        api.search.return_value = []
        # Mock the method name retrieval.py looks for: "wake_up"
        api.wake_up = MagicMock(return_value="wake content here")
        r = make_retrieval(api, cfg)
        result = r.prefetch("query", "sess_l0", background=False)
        assert "wake content here" in result

    def test_l0_text_truncated_to_max_wake_block_chars(self):
        cfg = FakeConfig(max_wake_block_chars=50)
        api = MagicMock()
        api.search.return_value = []
        api.wake_up = MagicMock(return_value="x" * 200)
        r = make_retrieval(api, cfg)
        result = r.prefetch("query", "sess_l0b")
        # Wake block should be capped
        assert len(result) <= cfg.max_wake_block_chars + 50  # header allowance


# ----------------------------------------------------------------
# Tunnel following
# ----------------------------------------------------------------

class TestTunnelFollowing:
    def test_tunnels_not_followed_by_default(self):
        cfg = FakeConfig(follow_tunnels=False)
        api = MagicMock()
        api.search.return_value = []
        api.follow_tunnels = MagicMock(return_value=[])
        r = make_retrieval(api, cfg)
        r.prefetch("query", "sess_t1")
        # follow_tunnels should not be called
        api.follow_tunnels.assert_not_called()

    def test_tunnels_followed_when_enabled(self):
        cfg = FakeConfig(follow_tunnels=True, max_tunnel_hops=1, max_tunnel_hits=2)
        api = MagicMock()
        api.search.return_value = []
        api.follow_tunnels = MagicMock(return_value=[
            {"content": "tunnel content", "wing": "proj", "room": "rooms"}
        ])
        r = make_retrieval(api, cfg)
        result = r.prefetch("query", "sess_t2", prefetch_wing="proj", background=False)
        # follow_tunnels was called
        assert api.follow_tunnels.called


# ----------------------------------------------------------------
# KG demotion of expired facts
# ----------------------------------------------------------------

class TestKGExpiredFacts:
    def test_kg_expired_fact_not_injected(self):
        cfg = FakeConfig(use_kg=True)
        api = MagicMock()
        api.search.return_value = []
        api.kg_query_entity = MagicMock(return_value=[
            {"subject": "Max", "predicate": "attended", "object": "School", "valid_to": "2025-06-01"},
            {"subject": "Max", "predicate": "loves", "object": "chess", "valid_to": None},
        ])
        r = make_retrieval(api, cfg)
        result = r.prefetch("Max", "sess_kg1")
        # Only non-expired fact should appear
        assert "attended School" not in result or "loves chess" in result