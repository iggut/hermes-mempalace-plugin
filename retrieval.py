"""MemPalace retrieval — staged L0/L1/L2/L3 recall pipeline with char budgets."""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------
# Lexical pattern definitions (exact-match boost targets)
# ----------------------------------------------------------------
_LEXICAL_PATTERNS = [
    re.compile(r"[/\.\\]"),           # file path separators
    re.compile(r"(?:^|[\s`\"'])(/[a-zA-Z0-9_./\-]+)"),  # /absolute/path
    re.compile(r"^[\w]{3,30}$"),     # simple identifiers
    re.compile(r"\d{3,}" ),           # long digit sequences (ports, etc.)
]

# Evidence strength thresholds
_STRONG_THRESHOLD = 0.75
_MEDIUM_THRESHOLD = 0.50


def _score_is_strong(score: float) -> bool:
    return score >= _STRONG_THRESHOLD


def _score_is_medium(score: float) -> bool:
    return score >= _MEDIUM_THRESHOLD


def _classify_evidence(query: str, hit: Dict[str, Any]) -> str:
    """Classify a hit as strong/medium/weak based on lexical + score."""
    content = (hit.get("content") or "").lower()
    q = query.lower()

    # Exact substring in content
    if q in content:
        return "strong"

    # Check lexical patterns
    for pat in _LEXICAL_PATTERNS:
        if pat.search(query) and pat.search(content):
            return "strong"

    score = hit.get("score", 0)
    if _score_is_strong(score):
        return "strong"
    if _score_is_medium(score):
        return "medium"
    return "weak"


# ----------------------------------------------------------------
# Timeout executor singleton
# ----------------------------------------------------------------

_TIMEOUT_EXECUTOR: Optional[ThreadPoolExecutor] = None
_EXECUTOR_LOCK = threading.Lock()


def _get_timeout_executor(max_workers: int = 4) -> ThreadPoolExecutor:
    global _TIMEOUT_EXECUTOR
    if _TIMEOUT_EXECUTOR is None:
        with _EXECUTOR_LOCK:
            if _TIMEOUT_EXECUTOR is None:
                _TIMEOUT_EXECUTOR = ThreadPoolExecutor(
                    max_workers=max_workers, thread_name_prefix="mempalace-retrieval-"
                )
    return _TIMEOUT_EXECUTOR


def _run_with_timeout(
    fn: Callable[[], Any], timeout_seconds: float, default: Any = None
) -> Any:
    """Run fn in a bounded thread pool with hard timeout."""
    try:
        executor = _get_timeout_executor()
        future = executor.submit(fn)
        return future.result(timeout=timeout_seconds)
    except FuturesTimeoutError:
        logger.debug("[MemPalaceRetrieval] timed out after %.3fs", timeout_seconds)
        return default
    except RuntimeError as e:
        logger.debug("[MemPalaceRetrieval] executor unavailable: %s", e)
        return default
    except Exception as e:
        logger.debug("[MemPalaceRetrieval] executor error: %s", e)
        return default


# ----------------------------------------------------------------
# Cache entry
# ----------------------------------------------------------------

@dataclass
class CacheEntry:
    result: str
    timestamp: float


class RetrievalCache:
    """LRU prefetch cache with TTL and stale fallback."""

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
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._misses += 1
                return None
            age = time.time() - entry.timestamp
            if age > self._ttl:
                self._misses += 1
                return None
            self._hits += 1
            return entry.result

    def get_stale(self, key: Any) -> Optional[str]:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            age = time.time() - entry.timestamp
            if age > self._ttl:
                self._stale_hits += 1
            return entry.result

    def set(self, key: Any, result: str) -> None:
        with self._lock:
            if len(self._cache) >= self._max_size and key not in self._cache:
                oldest_key = min(self._cache, key=lambda k: self._cache[k].timestamp)
                del self._cache[oldest_key]
                self._evictions += 1
            self._cache[key] = CacheEntry(result=result, timestamp=time.time())

    def prefetch_key(
        self, query: str, session_id: str = "", wing: str = "", room: str = ""
    ) -> Any:
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
    """Staged recall engine: L0 wake block → L1 mstack → L2 scoped → L3 hybrid.

    All limits are enforced via config (see MemPalaceConfig):
      - max_wake_block_chars   L0 cap
      - max_recall_chars       total recall block cap
      - max_quote_chars_per_hit  per-hit content cap
      - max_total_quoted_chars  aggregate quoted chars cap
      - max_l3_search_time_ms   L3 timeout
      - follow_tunnels / max_tunnel_hops / max_tunnel_hits  tunnel cap
    """

    def __init__(
        self,
        api,  # MemPalaceAPI instance
        config,  # MemPalaceConfig instance
        metric_fn: Optional[Callable[[str], None]] = None,
    ):
        self._api = api
        self._config = config
        self._metric = metric_fn or (lambda _name: None)
        self._cache = RetrievalCache(
            max_size=config.prefetch_cache_size,
            ttl_seconds=config.cache_ttl_seconds,
        )
        self._diag: Dict[str, Any] = {
            "l0_wake_hits": 0,
            "l1_mstack_hits": 0,
            "l2_scoped_searches": 0,
            "l3_hybrid_searches": 0,
            "tunnel_follows": 0,
            "kg_lookups": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "recall_chars_injected": 0,
            "hits_dropped_token_cap": 0,
            "weak_omissions": 0,
            "timeouts": 0,
            "ingestion_attempts": 0,
            "ingestion_skips": 0,
            "ingestion_dup_skips": 0,
            "ingestion_errors": 0,
        }

    # ----------------------------------------------------------------
    # Public API (backwards-compatible signatures)
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
        """Main entry point — run staged recall pipeline.

        Returns a compact "Relevant MemPalace recall:" block capped by
        max_recall_chars, with per-hit quote capped by max_quote_chars_per_hit.
        """
        key = self._cache.prefetch_key(query, session_id, prefetch_wing, prefetch_room)

        # Fresh cache
        cached = self._cache.get(key)
        if cached is not None:
            self._diag["cache_hits"] += 1
            return cached

        # Stale fallback
        stale = self._cache.get_stale(key)
        if stale is not None:
            self._diag["cache_hits"] += 1
            self._metric("stale_cache_hits")
            if background:
                self._start_inflight_refresh(key, query, prefetch_wing, prefetch_room)
            return stale

        # Live
        self._diag["cache_misses"] += 1
        return self._prefetch_live(
            key, query, session_id, prefetch_wing, prefetch_room, background
        )

    def queue_prefetch(
        self,
        query: str,
        session_id: str = "",
        *,
        prefetch_wing: str = "",
        prefetch_room: str = "",
    ) -> None:
        if not self._config.background_retrieval:
            return
        key = self._cache.prefetch_key(query, session_id, prefetch_wing, prefetch_room)
        if key in self._cache._cache:
            return
        t = threading.Thread(
            target=self._prefetch_live,
            args=(key, query, session_id, prefetch_wing, prefetch_room, True),
            daemon=True,
        )
        t.start()

    def _start_inflight_refresh(
        self, key: Any, query: str, wing: str, room: str
    ) -> None:
        t = threading.Thread(
            target=self._prefetch_live,
            args=(key, query, "", wing, room, True),
            daemon=True,
        )
        t.start()

    # ----------------------------------------------------------------
    # Staged pipeline
    # ----------------------------------------------------------------

    def _prefetch_live(
        self,
        key: Any,
        query: str,
        session_id: str,
        wing: str,
        room: str,
        background: bool,
    ) -> str:
        """Run the L0→L1→L2→L3 pipeline and return formatted recall block."""
        if background and self._config.background_retrieval:
            t = threading.Thread(
                target=self._fetch_and_cache,
                args=(key, query, session_id, wing, room),
                daemon=True,
            )
            t.start()
            return ""

        return self._fetch_with_timeout(key, query, session_id, wing, room)

    def _fetch_and_cache(
        self, key: Any, query: str, session_id: str, wing: str, room: str
    ) -> None:
        result = self._fetch_with_timeout(key, query, session_id, wing, room)
        if result:
            self._cache.set(key, result)

    def _fetch_with_timeout(
        self, key: Any, query: str, session_id: str, wing: str, room: str = "",
        **kwargs,  # backward compat: old callers pass timeout= as keyword
    ) -> str:
        """Execute staged pipeline with timeouts at each stage.

        Backward compatibility: wing/room may come from key tuple (old callers)
        or from explicit parameters (new callers). We resolve both.
        """
        all_hits: List[Dict[str, Any]] = []
        total_chars = 0

        # Backward compat: extract wing/room from cache key tuple (key[2], key[3])
        # if explicit room parameter is empty (old callers did this).
        # New callers pass explicit wing/room as 4th/5th positional args.
        if isinstance(key, tuple) and len(key) >= 4:
            key_wing = key[2] if key[2] else wing
            key_room = key[3] if key[3] else room
        else:
            key_wing = wing
            key_room = room

        # L0: tiny wake block — always runs if api supports it
        l0_text = self._run_l0_wake_block()
        if l0_text:
            total_chars += len(l0_text)

        # L1: memory stack scoped recall
        l1_hits = self._run_l1_mstack(key_wing, key_room)
        all_hits.extend(l1_hits)

        # L2: targeted scoped recall (use key-based wing/room for compat)
        l2_hits = self._run_l2_scoped_recall(query, session_id, key_wing, key_room)
        all_hits.extend(l2_hits)

        # L3: full hybrid search fallback
        l3_hits = self._run_l3_hybrid_search(query, key_wing, key_room)
        all_hits.extend(l3_hits)

        # Deduplicate by drawer_id
        seen_ids: Set[str] = set()
        deduped: List[Dict[str, Any]] = []
        for hit in all_hits:
            did = hit.get("drawer_id") or hit.get("source_file") or str(id(hit))
            if did not in seen_ids:
                seen_ids.add(did)
                deduped.append(hit)

        # Sort by score descending
        deduped.sort(key=lambda h: h.get("score", 0), reverse=True)

        # Format with char budget
        recall_block = self._format_recall_block(query, deduped, l0_text)

        self._diag["recall_chars_injected"] = len(recall_block)

        if recall_block:
            self._cache.set(key, recall_block)

        return recall_block

    # ----------------------------------------------------------------
    # L0 — tiny wake context
    # ----------------------------------------------------------------

    def _run_l0_wake_block(self) -> str:
        """Get the minimal memory stack wake block if available."""
        try:
            wake_fn = getattr(self._api, "wake_up", None)
            if not callable(wake_fn):
                return ""
            result = _run_with_timeout(wake_fn, timeout_seconds=1.0)
            if not result:
                return ""
            if isinstance(result, str):
                text = result
            elif isinstance(result, dict):
                text = result.get("content", result.get("text", ""))
            else:
                text = str(result)
            cap = self._config.max_wake_block_chars
            if len(text) > cap:
                text = text[:cap]
            if text:
                self._diag["l0_wake_hits"] += 1
            return text
        except Exception as e:
            logger.debug("[MemPalaceRetrieval] L0 wake_up failed: %s", e)
            return ""

    # ----------------------------------------------------------------
    # L1 — memory stack scoped recall
    # ----------------------------------------------------------------

    def _run_l1_mstack(self, wing: str, room: str) -> List[Dict[str, Any]]:
        """Memory stack scoped recall if enabled."""
        if not self._config.memory_stack_enabled:
            return []
        use_wing = wing or self._config.wake_up_wing or self._config.target_wing or ""
        if not use_wing:
            return []
        try:
            scoped_fn = getattr(self._api, "scoped_recall", None)
            if not callable(scoped_fn):
                return []
            result = scoped_fn(
                use_wing,
                room=room or self._config.l2_default_room or None,
                char_budget=self._config.recall_char_budget,
            )
            if result:
                self._diag["l1_mstack_hits"] += 1
            # Parse structured result or wrap raw text as a hit
            if isinstance(result, list):
                return result
            if isinstance(result, dict):
                return [result]
            if isinstance(result, str) and result.strip():
                return [{
                    "content": result[: self._config.recall_char_budget],
                    "source": "mstack_scoped_recall",
                    "score": 1.0,
                    "wing": use_wing,
                    "room": room or self._config.l2_default_room or "",
                }]
            return []
        except Exception as e:
            logger.debug("[MemPalaceRetrieval] L1 mstack failed: %s", e)
            return []

    # ----------------------------------------------------------------
    # L2 — targeted scoped recall
    # ----------------------------------------------------------------

    def _run_l2_scoped_recall(
        self, query: str, session_id: str, wing: str, room: str
    ) -> List[Dict[str, Any]]:
        """Targeted recall with wing/room scoping, KG, halls, closets, tunnels."""
        self._diag["l2_scoped_searches"] += 1
        self._metric("l2_recalls")  # backward compat with existing tests
        hits: List[Dict[str, Any]] = []

        # Determine active wing/room
        use_wing = wing or self._config.wake_up_wing or ""
        use_room = room or self._config.l2_default_room or ""

        # KG lookup if enabled (use_kg is the new flag; include_kg_facts is the legacy flag)
        if self._config.use_kg or self._config.include_kg_facts:
            kg_hits = self._run_kg_lookup(query)
            hits.extend(kg_hits)

        # Tunnel following if enabled
        if self._config.follow_tunnels and use_wing:
            tunnel_hits = self._run_tunnel_follow(use_wing, use_room)
            hits.extend(tunnel_hits)

        # Scoped semantic search
        if use_wing:
            scoped_hits = self._search(
                query,
                wing=use_wing,
                room=use_room or None,
                limit=min(self._config.max_results, 5),
                timeout_ms=min(self._config.max_l3_search_time_ms, 300),
            )
            hits.extend(scoped_hits)

        return hits

    def _run_kg_lookup(self, query: str) -> List[Dict[str, Any]]:
        """Extract entity hints from query and query KG for facts."""
        self._diag["kg_lookups"] += 1
        hits: List[Dict[str, Any]] = []
        try:
            kg_query_fn = getattr(self._api, "kg_query_entity", None)
            if not callable(kg_query_fn):
                return []
            words = [w.strip(".,?!:;\"'") for w in query.split()]
            entities = [
                w for w in words
                if w and len(w) > 2 and w[0].isupper()
            ][: self._config.kg_entity_limit]
            for entity in entities:
                triples = kg_query_fn(entity)
                if not triples:
                    continue
                for t in triples:
                    # Demote invalidated/expired facts
                    if t.get("valid_to"):
                        continue
                    subject = t.get("subject", "?")
                    predicate = t.get("predicate", "?")
                    obj = t.get("object", "?")
                    text = f"{subject} {predicate} {obj}"
                    hits.append({
                        "content": text,
                        "source": "kg",
                        "score": 0.6,  # KG facts are medium confidence
                        "entity": entity,
                    })
        except Exception as e:
            logger.debug("[MemPalaceRetrieval] KG lookup failed: %s", e)
        return hits

    def _run_tunnel_follow(self, wing: str, room: str) -> List[Dict[str, Any]]:
        """Follow cross-wing tunnels up to max_tunnel_hops and max_tunnel_hits."""
        self._diag["tunnel_follows"] += 1
        hits: List[Dict[str, Any]] = []
        try:
            tunnels_fn = getattr(self._api, "follow_tunnels", None)
            if not callable(tunnels_fn):
                return []
            traversed = tunnels_fn(
                room=room or self._config.l2_default_room or "",
                wing=wing,
                max_hops=self._config.max_tunnel_hops,
            )
            if not traversed:
                return []
            for node in traversed[: self._config.max_tunnel_hits]:
                content = node.get("content", "") or node.get("text", "")
                if not content:
                    continue
                hits.append({
                    "content": content[: self._config.max_quote_chars_per_hit],
                    "source": "tunnel",
                    "score": 0.55,
                    "wing": node.get("wing", wing),
                    "room": node.get("room", ""),
                })
        except Exception as e:
            logger.debug("[MemPalaceRetrieval] tunnel follow failed: %s", e)
        return hits

    # ----------------------------------------------------------------
    # L3 — full hybrid search fallback
    # ----------------------------------------------------------------

    def _run_l3_hybrid_search(
        self, query: str, wing: str = "", room: str = ""
    ) -> List[Dict[str, Any]]:
        """Full search with timeout. Used as fallback when L2 is weak."""
        self._diag["l3_hybrid_searches"] += 1
        self._metric("l3_searches")  # backward compat with existing tests
        # Use per-stage timeout (max_l3_search_time_ms) but also honor
        # retrieval_timeout_ms as a hard cap for the entire L3 operation
        l3_timeout = min(self._config.max_l3_search_time_ms, self._config.retrieval_timeout_ms)
        return self._search(
            query,
            wing=wing,
            room=room or None,
            limit=self._config.max_results,
            timeout_ms=l3_timeout,
        )

    def _search(
        self,
        query: str,
        wing: str = "",
        room: Optional[str] = None,
        limit: int = 8,
        timeout_ms: int = 400,
    ) -> List[Dict[str, Any]]:
        """Run API search with hard timeout."""
        timeout_s = max(0.05, timeout_ms / 1000.0)

        def _call():
            return self._api.search(
                query,
                wing=wing or None,
                room=room or None,
                limit=limit,
                min_score=self._config.min_score,
            )

        results = _run_with_timeout(_call, timeout_seconds=timeout_s, default=None)
        if results is None:
            self._diag["timeouts"] += 1
            self._metric("retrieval_timeouts")  # backward compat with existing tests
            return []
        return results if isinstance(results, list) else []

    # ----------------------------------------------------------------
    # Formatting with char/token budgets
    # ----------------------------------------------------------------

    def _format_recall_block(
        self, query: str, hits: List[Dict[str, Any]], l0_text: str
    ) -> str:
        """Build the compact recall block within char budgets.

        Format:
          Relevant MemPalace recall:
          - [strong] <snippet> — source=<src>, wing=<wing>, room=<room>
          - [medium] <snippet> — source=<src>, wing=<wing>, room=<room>
          - [weak] (omitted — low confidence)

        Rules:
          - total block ≤ max_recall_chars
          - per hit content ≤ max_quote_chars_per_hit
          - total quoted chars ≤ max_total_quoted_chars
          - weak hits omitted from block entirely
        """
        max_total = self._config.max_recall_chars
        max_per_hit = self._config.max_quote_chars_per_hit
        max_quoted = self._config.max_total_quoted_chars

        parts: List[str] = []
        quoted_chars = 0
        dropped = 0

        # L0 wake block first (already counted in total but is prose, not a hit)
        if l0_text:
            # L0 is a prose block, not a hit list — just append it
            parts.append(f"[wake-up]\n{l0_text}")

        # Insert KG header before first KG-sourced hit (backward compat)
        kg_started = False
        for hit in hits:
            evidence = _classify_evidence(query, hit)
            if evidence == "weak":
                self._diag["weak_omissions"] += 1
                dropped += 1
                continue

            content_raw = hit.get("content", "")
            if not content_raw:
                continue

            # Insert KG section header before first KG hit (backward compat)
            hit_src = hit.get("source", hit.get("source_file", ""))
            is_kg = hit_src == "kg" or hit.get("entity") is not None
            if is_kg and not kg_started:
                kg_hdr = "--- Knowledge Graph ---"
                if sum(len(p) + 1 for p in parts) + len(kg_hdr) + 1 <= max_total:
                    parts.append(kg_hdr)
                    kg_started = True

            # Per-hit cap
            content = content_raw[:max_per_hit]
            if len(content_raw) > max_per_hit:
                content = content.rstrip() + "..."

            # Total quoted chars cap
            if quoted_chars + len(content) > max_quoted:
                self._diag["hits_dropped_token_cap"] += 1
                dropped += 1
                continue

            # Build entry
            score = hit.get("score", 0)
            wing = hit.get("wing", "")
            hit_room = hit.get("room", "")
            src = hit.get("source_file", hit.get("source", hit.get("drawer_id", "?")))
            date = hit.get("date", hit.get("created_at", ""))

            meta_parts = []
            if src and src != "?":
                meta_parts.append(f"source={src}")
            if wing:
                meta_parts.append(f"wing={wing}")
            if hit_room:
                meta_parts.append(f"room={hit_room}")
            if date:
                meta_parts.append(f"date={date}")
            meta_str = ", ".join(meta_parts)

            line = f"- [{evidence}] {content}"
            if meta_str:
                line += f" — {meta_str}"

            # Total block cap
            if sum(len(p) + 1 for p in parts) + len(line) + 1 > max_total:
                self._diag["hits_dropped_token_cap"] += 1
                dropped += 1
                continue

            parts.append(line)
            quoted_chars += len(content)

        if not parts:
            return ""

        return "Relevant MemPalace recall:\n" + "\n".join(parts)

    # ----------------------------------------------------------------
    # Diagnostics
    # ----------------------------------------------------------------

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "cache": self._cache.stats(),
            "staged_pipeline": dict(self._diag),
            "config": {
                "max_wake_block_chars": self._config.max_wake_block_chars,
                "max_recall_chars": self._config.max_recall_chars,
                "max_quote_chars_per_hit": self._config.max_quote_chars_per_hit,
                "max_total_quoted_chars": self._config.max_total_quoted_chars,
                "max_l3_search_time_ms": self._config.max_l3_search_time_ms,
                "follow_tunnels": self._config.follow_tunnels,
                "max_tunnel_hops": self._config.max_tunnel_hops,
                "max_tunnel_hits": self._config.max_tunnel_hits,
                "use_kg": self._config.use_kg,
                "prefer_active_project": self._config.prefer_active_project,
                "memory_stack_enabled": self._config.memory_stack_enabled,
            },
        }

    def _record_ingestion(
        self,
        attempts: int = 0,
        skips: int = 0,
        dup_skips: int = 0,
        errors: int = 0,
    ) -> None:
        self._diag["ingestion_attempts"] += attempts
        self._diag["ingestion_skips"] += skips
        self._diag["ingestion_dup_skips"] += dup_skips
        self._diag["ingestion_errors"] += errors