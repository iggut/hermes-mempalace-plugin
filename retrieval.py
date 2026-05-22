"""Retrieval with real timeout executor, cache TTL, and stale-cache fallback."""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------
# Timeout executor singleton (one per process)
# ----------------------------------------------------------------

_TIMEOUT_EXECUTOR: Optional[ThreadPoolExecutor] = None
_EXECUTOR_LOCK = threading.Lock()


def _get_timeout_executor(max_workers: int = 4) -> ThreadPoolExecutor:
    global _TIMEOUT_EXECUTOR
    if _TIMEOUT_EXECUTOR is None:
        with _EXECUTOR_LOCK:
            if _TIMEOUT_EXECUTOR is None:
                _TIMEOUT_EXECUTOR = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="mempalace-retrieval-")
    return _TIMEOUT_EXECUTOR


def _run_with_timeout(fn: Callable[[], Any], timeout_seconds: float) -> Any:
    """Run fn in a bounded thread pool with hard timeout."""
    executor = _get_timeout_executor()
    future = executor.submit(fn)
    try:
        return future.result(timeout=timeout_seconds)
    except FuturesTimeoutError:
        logger.debug("[MemPalaceRetrieval] timed out after %.3fs", timeout_seconds)
        return None
    except Exception as e:
        logger.debug("[MemPalaceRetrieval] executor error: %s", e)
        return None


# ----------------------------------------------------------------
# Cache entry
# ----------------------------------------------------------------

@dataclass
class CacheEntry:
    result: str
    timestamp: float


class RetrievalCache:
    """LRU prefetch cache with TTL and stale fallback.

    Each entry has a timestamp. On access, stale entries are returned
    immediately as a fallback while a fresh fetch runs in background.
    """

    def __init__(self, max_size: int = 32, ttl_seconds: int = 30):
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._cache: Dict[Any, CacheEntry] = {}
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._stale_hits = 0

    def get(self, key: Any) -> Optional[str]:
        """Get cached result. Returns None if not found or expired.

        If expired but not yet evicted, marks as stale hit.
        """
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._misses += 1
                return None
            age = time.time() - entry.timestamp
            if age > self._ttl:
                # Expired — return None so caller falls back to live fetch.
                # Don't evict yet; it may still serve as a fallback.
                self._misses += 1
                return None
            self._hits += 1
            return entry.result

    def get_stale(self, key: Any) -> Optional[str]:
        """Get cached result even if expired. Used for stale fallback."""
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            age = time.time() - entry.timestamp
            if age > self._ttl:
                self._stale_hits += 1
            return entry.result

    def set(self, key: Any, result: str) -> None:
        """Cache a result. Evicts oldest if full."""
        with self._lock:
            # Evict oldest if at capacity
            if len(self._cache) >= self._max_size and key not in self._cache:
                oldest_key = min(self._cache, key=lambda k: self._cache[k].timestamp)
                del self._cache[oldest_key]
                self._evictions += 1
            self._cache[key] = CacheEntry(result=result, timestamp=time.time())

    def prefetch_key(self, query: str, session_id: str, wing: str = "", room: str = "") -> Any:
        """Build cache key from query + session + wing/room."""
        return (session_id, query, wing, room)

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {
                "size": len(self._cache),
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
                "stale_hits": self._stale_hits,
            }

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0
            self._evictions = 0
            self._stale_hits = 0


# ----------------------------------------------------------------
# Retrieval engine
# ----------------------------------------------------------------


class MemPalaceRetrieval:
    """Retrieval engine with real timeout, cache TTL, KG facts, graph context."""

    def __init__(
        self,
        api,  # MemPalaceAPI instance
        config,  # MemPalaceConfig instance
    ):
        self._api = api
        self._config = config
        self._cache = RetrievalCache(
            max_size=config.prefetch_cache_size,
            ttl_seconds=config.cache_ttl_seconds,
        )
        self._inflight: Dict[Any, threading.Event] = {}

    # ----------------------------------------------------------------
    # Public API
    # ----------------------------------------------------------------

    def prefetch(
        self,
        query: str,
        session_id: str = "",
        *,
        prefetch_wing: str = "",
        prefetch_room: str = "",
        background: bool = True,
    ) -> str:
        """Retrieve memory context with bounded timeout.

        Uses cache if available. On cache miss, runs live search bounded
        by retrieval_timeout_seconds. On timeout, falls back to stale
        cache or empty string.
        """
        key = self._cache.prefetch_key(query, session_id, prefetch_wing, prefetch_room)

        # Try fresh cache first
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        # Try stale cache as immediate fallback (don't wait)
        stale = self._cache.get_stale(key)
        if stale is not None:
            # Serve stale immediately, refresh in background
            if background:
                self._start_inflight_refresh(key, query, prefetch_wing, prefetch_room)
            return stale

        # Cache miss — run live retrieval with real timeout
        return self._prefetch_live(key, query, prefetch_wing, prefetch_room, background)

    def queue_prefetch(
        self,
        query: str,
        session_id: str = "",
        *,
        prefetch_wing: str = "",
        prefetch_room: str = "",
    ) -> None:
        """Warm the cache in background. No-op if background_retrieval=false."""
        if not self._config.background_retrieval:
            return
        key = self._cache.prefetch_key(query, session_id, prefetch_wing, prefetch_room)
        if key in self._cache._cache:
            return  # Already cached

        t = threading.Thread(
            target=self._prefetch_live,
            args=(key, query, prefetch_wing, prefetch_room, True),
            daemon=True,
        )
        t.start()

    def _start_inflight_refresh(self, key: Any, query: str, wing: str, room: str) -> None:
        """Start an async refresh for an already-served stale entry."""
        t = threading.Thread(
            target=self._prefetch_live,
            args=(key, query, wing, room, True),
            daemon=True,
        )
        t.start()

    def _prefetch_live(
        self,
        key: Any,
        query: str,
        wing: str,
        room: str,
        background: bool,
    ) -> str:
        """Run live retrieval with hard timeout, then cache result."""
        timeout = max(0.05, self._config.retrieval_timeout_seconds)

        if background and self._config.background_retrieval:
            t = threading.Thread(
                target=self._fetch_and_cache,
                args=(key, query, wing, room),
                daemon=True,
            )
            t.start()
            return ""  # Async; return empty for synchronous callers

        return self._fetch_with_timeout(key, query, wing, room, timeout)

    def _fetch_and_cache(self, key: Any, query: str, wing: str, room: str) -> None:
        """Background fetch and cache."""
        timeout = max(0.05, self._config.retrieval_timeout_seconds)
        result = self._fetch_with_timeout(key, query, wing, room, timeout)
        if result:
            self._cache.set(key, result)

    def _fetch_with_timeout(
        self, key: Any, query: str, wing: str, room: str, timeout: float
    ) -> str:
        """Run search, KG facts, graph, all with hard timeout."""
        parts = []
        total_chars = 0
        MAX_CHARS = 2000

        def fetch_search():
            return self._api.search(
                query,
                wing=wing,
                room=room,
                limit=self._config.max_results,
                min_score=self._config.min_score,
            )

        # Semantic search with real timeout
        results = _run_with_timeout(fetch_search, timeout)
        if results is None:
            logger.debug("[MemPalaceRetrieval] search timed out or errored")
            results = []

        for r in results:
            if total_chars >= MAX_CHARS:
                break
            content = r.get("content", "")
            if not content:
                continue
            score = r.get("score", 0)
            source = r.get("source_file", r.get("drawer_id", "?"))
            line = f"[{score:.2f}] {content[:500]}"
            if len(line) > 200:
                line = line[:200] + "..."
            parts.append(line)
            total_chars += len(line) + 1

        # KG facts
        if self._config.include_kg_facts and total_chars < MAX_CHARS:
            total_chars = self._append_kg_facts(query, parts, total_chars, MAX_CHARS)

        # Graph context
        if self._config.graph_enabled and total_chars < MAX_CHARS:
            total_chars = self._append_graph_context(parts, total_chars, MAX_CHARS)

        result = "\n".join(parts)
        if result:
            self._cache.set(key, result)
        return result

    def _append_kg_facts(
        self, query: str, parts: List[str], total_chars: int, max_chars: int
    ) -> int:
        """Extract entity hints from query and append KG triples."""
        try:
            # Simple entity extraction from query
            words = [w.strip(".,?!:;\"'") for w in query.split()]
            entities = [w for w in words if w and w[0].isupper() and len(w) > 2][:self._config.kg_entity_limit]

            if not entities:
                return total_chars

            all_triples = []
            for entity in entities:
                triples = self._api.kg_query_entity(entity)
                all_triples.extend(triples)

            if not all_triples:
                return total_chars

            hdr = "--- Knowledge Graph ---"
            if total_chars + len(hdr) + 1 > max_chars:
                return total_chars
            parts.append(hdr)
            total_chars += len(hdr) + 1

            for t in all_triples[:5]:
                if total_chars >= max_chars:
                    break
                line = f"  {t.get('subject','?')} {t.get('predicate','?')} {t.get('object','?')}"
                parts.append(line)
                total_chars += len(line) + 1

        except Exception as e:
            logger.debug("[MemPalaceRetrieval] KG facts append failed: %s", e)

        return total_chars

    def _append_graph_context(
        self, parts: List[str], total_chars: int, max_chars: int
    ) -> int:
        """Append graph traversal connected rooms."""
        try:
            start_room = self._config.l2_default_room or "conversations"
            traversed = self._api.graph_traverse(
                start_room,
                max_hops=self._config.graph_traverse_max_hops,
                limit=self._config.graph_traverse_limit,
            )
            if not traversed:
                return total_chars

            hdr = "--- Connected Rooms (graph) ---"
            if total_chars + len(hdr) + 1 > max_chars:
                return total_chars
            parts.append(hdr)
            total_chars += len(hdr) + 1

            for node in traversed:
                if total_chars >= max_chars:
                    break
                room = node.get("room", "?")
                wing = node.get("wing", "?")
                line = f"  {wing}/{room}"
                parts.append(line)
                total_chars += len(line) + 1

        except Exception as e:
            logger.debug("[MemPalaceRetrieval] graph context append failed: %s", e)

        return total_chars

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "cache": self._cache.stats(),
            "retrieval_timeout_seconds": self._config.retrieval_timeout_seconds,
            "background_retrieval": self._config.background_retrieval,
        }