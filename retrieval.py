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
# Patterns to extract specific token types from queries.
# Each pattern returns a list of concrete token strings (normalized).
# A hit is "strong" ONLY when at least one extracted query token
# appears verbatim (not just same type) in the hit content.

# Path patterns: require at least one path separator (/) or drive letter (\)
# to avoid matching single generic words that happen to appear in queries.
_PATH_ABS_RE = re.compile(
    r"(?:^|[\s`\"'(<[])((?:/[a-zA-Z0-9_.\-]+)+|"
    r"[A-Za-z]:\\[a-zA-Z0-9_.\-]+(?:\\[a-zA-Z0-9_.\-]+)*)"
)
# Tilde paths: require ~/
_PATH_TILDE_RE = re.compile(r"(?:^|(?<![\w/]))~/[a-zA-Z0-9_./-]+", re.IGNORECASE)
# Relative paths: require ./ or ../ prefix, OR embedded / separator in a path-like token
_PATH_REL_RE = re.compile(r"(?:^|[\s`\"'(<[])((?:\.\.?/)?[a-zA-Z0-9_.\-/]+)")
# Bare filenames: must contain a dot + extension, and either
# (a) preceded by path separator or space, OR (b) followed by separator/space/end
# This prevents matching single words like "port" just because they appear near a dot
_BARE_FNAME_RE = re.compile(r'\b([a-zA-Z0-9_\-]+\.[a-zA-Z]{2,10})\b')

# Generic identifiers: words that should NOT contribute to strong classification
# when high-specificity tokens (path/port/model/config/quoted) are also present.
_IDENTIFIER_STOPWORDS = frozenset({
    # Common English words that appear in code queries
    "the", "and", "for", "with", "from", "this", "that",
    "are", "was", "were", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "must",
    "into", "over", "under", "about", "out", "up", "down",
    # Common English verbs/adjectives that appear in code queries
    "is", "am", "be", "being", "been",
    "fail", "failing", "fails", "failed", "failure",
    "work", "working", "works", "worked",
    "run", "running", "runs", "ran",
    "set", "setting", "sets", "setup",
    "get", "getting", "gets", "got",
    "make", "making", "makes", "made",
    "use", "using", "uses", "used",
    "see", "seeing", "seen", "saw",
    "know", "knowing", "known", "knew",
    "want", "wanting", "wanted",
    "need", "needing", "needed",
    "change", "changing", "changed", "changes",
    "update", "updating", "updated", "updates",
    "add", "adding", "added", "adds",
    "remove", "removing", "removed", "removes",
    "check", "checking", "checked", "checks",
    "test", "testing", "tested", "tests",
    "start", "starting", "started", "starts",
    "stop", "stopping", "stopped", "stops",
    "open", "opening", "opened", "opens",
    "close", "closing", "closed", "closes",
    "read", "reading", "reads",
    "write", "writing", "writes", "wrote",
    # Generic technical terms that should NOT drive strong classification
    "port", "host", "path", "file", "name", "value", "key",
    "data", "text", "line", "code", "class", "func", "method",
    "config", "server", "client", "app", "service",
    "error", "err", "warning",
    "invalid", "wrong", "bad", "good", "ok",
    "type", "kind", "sort",
})

# Single-token identifiers: function names, class names, variable names
# Strict length requirement to avoid matching generic english words.
_IDENT_RE = re.compile(r"(?:^|[\s`\"'(<[],.])[a-zA-Z_][a-zA-Z0-9_]{3,30}(?=[`\"'\s)<\[].,]|$)")
# Port numbers — bare (8080) or with host prefix (localhost:8080, 127.0.0.1:8080)
_PORT_RE = re.compile(
    r"(?:(?:localhost|127[.]0[.]0[.]1|0[.]0[.]0[.]0)[:.](\d{4,5})|(?:^|[^\w])(\d{4,5})(?=[\s`\"'\s)<\[\].,;:]|$))"
)
# Model slugs: provider/model-name (e.g. hathor_rp-v.01-l3-8b-i1)
_MODEL_RE = re.compile(r"\b([a-zA-Z0-9_\-]+\/[a-zA-Z0-9_\-]+)\b")
# Config keys: dot.separated.keys or KEY_NAME — must have at least one dot
# to distinguish from generic identifiers. Single words like "port" or "fix"
# are not config keys even if they match the pattern. Capturing group 1 = full key.
_CONFIG_RE = re.compile(
    r"\b([a-zA-Z_][a-zA-Z0-9_]{0,30}"
    r"(?:\.[a-zA-Z_][a-zA-Z0-9_]+){1,3})\b"
)
# Error substrings in double-quoted strings from the query
_QUOTED_RE = re.compile(r"\"([^\"]{3,80})\"")


def _extract_query_tokens(query: str) -> Dict[str, Set[str]]:
    """Extract concrete lexical tokens from the query, grouped by type.

    Returns a dict of token_type -> set of normalized token strings.
    Each token type is tracked separately so we can check the SAME token
    appears in content, not just any token of the same type.

    IMPORTANT: Path tokens must have path separators (forward or backslash) to avoid
        extracting generic words like "port" as if they were file paths.
    """
    q = query
    tokens: Dict[str, Set[str]] = {
        "path": set(),
        "identifier": set(),
        "port": set(),
        "model": set(),
        "config": set(),
        "quoted": set(),
    }

    # Absolute paths: must start with / or drive letter
    for m in _PATH_ABS_RE.finditer(q):
        tok = m.group(1) if m.lastindex else m.group(0)
        if "/" in tok or "\\" in tok:
            tokens["path"].add(tok.lower())

    # Tilde paths
    for m in _PATH_TILDE_RE.finditer(q):
        tokens["path"].add(m.group(0).lower())

    # Relative paths: must have ./ or ../ or embedded /
    for m in _PATH_REL_RE.finditer(q):
        tok = m.group(1) if m.lastindex else m.group(0)
        if "/" in tok or "\\" in tok:
            tokens["path"].add(tok.lower())

    # Bare filenames: must have . + extension, path context already handled above
    for m in _BARE_FNAME_RE.finditer(q):
        tok = m.group(1).lower()
        if "/" not in tok and "\\" not in tok and ".." not in tok:
            tokens["path"].add(tok)

    # Identifiers: strict length + filter stopwords
    for m in _IDENT_RE.finditer(q):
        tok = m.group(0).lower()
        if tok not in _IDENTIFIER_STOPWORDS:
            tokens["identifier"].add(tok)

    # Port numbers
    for m in _PORT_RE.finditer(q):
        port = m.group(1) or m.group(2)
        if port:
            tokens["port"].add(port)

    # Model slugs
    for m in _MODEL_RE.finditer(q):
        tokens["model"].add(m.group(1).lower())

    # Config keys
    for m in _CONFIG_RE.finditer(q):
        tok = m.group(1).lower()
        # Filter out generic words that should not drive strong classification
        if tok not in _IDENTIFIER_STOPWORDS:
            tokens["config"].add(tok)

    # Quoted substrings
    for m in _QUOTED_RE.finditer(q):
        tokens["quoted"].add(m.group(1).lower())

    return tokens


def _token_in_content(token_type: str, tokens: Dict[str, Set[str]], content_lower: str) -> bool:
    """Check if any extracted token of the given type appears in content.

    For paths, identifiers, ports, models, config: check if the normalized
    token string appears in content_lower.
    For 'port' tokens: use word-boundary check to avoid "8080" matching inside "9090".
    For 'quoted': check if the exact quoted phrase appears.
    """
    group = tokens.get(token_type, set())
    if not group:
        return False
    for tok in group:
        if token_type == "port":
            # Use word boundaries for port numbers to avoid "8080" matching inside "9090"
            import re as _re
            if _re.search(r"(?:^|[^\d])" + _re.escape(tok) + r"(?:$|[^\d])", content_lower):
                return True
        elif token_type == "model":
            # Model slugs like "hathor_rp-v.01-l3-8b-i1" must match as distinct tokens
            # Use word boundaries to prevent "provider/model" matching inside a path
            import re as _re2
            if _re2.search(r"(?:^|[^\w\/])" + _re2.escape(tok) + r"(?:$|[^\w\/])", content_lower):
                return True
        elif tok in content_lower:
            return True
    return False


def _classify_evidence(query: str, hit: Dict[str, Any]) -> str:
    """Classify a hit as strong/medium/weak based on lexical + score.

    A hit is STRONG only when:
      1. The full normalized query is an exact substring of the content, OR
      2. The query contains high-specificity tokens (path/port/model/config/quoted)
         AND at least one of those specific tokens appears verbatim in the content, OR
      3. The query contains NO high-specificity tokens
         AND at least one extracted token (any type, including identifiers) appears
         in the content, OR
      4. Score >= 0.75 (fallback when no token matches, NON-EMPTY QUERY ONLY)

    A hit is MEDIUM when score >= 0.50 but no token match.
    A hit is WEAK otherwise.
    """
    content_lower = (hit.get("content") or "").lower()
    q = query.lower()

    # Exact query substring match (skip empty query to avoid "" matching everything)
    if q and q in content_lower:
        return "strong"

    # Extract concrete tokens from query
    qt = _extract_query_tokens(query)

    # Specificity gate: classify high-specificity tokens vs generic identifiers
    HIGH_SPECIFICITY_TYPES = ("path", "port", "model", "config", "quoted")
    has_high_spec = any(qt.get(tt, set()) for tt in HIGH_SPECIFICITY_TYPES)

    if has_high_spec:
        # STRONG only when at least one HIGH-SPECIFICITY token matches exactly.
        # Generic identifier matches do NOT count when high-spec tokens exist.
        for ttype in HIGH_SPECIFICITY_TYPES:
            if _token_in_content(ttype, qt, content_lower):
                return "strong"
        # High-spec tokens present but none matched — score-based only (medium/weak)
        score = hit.get("score", 0)
        if score >= 0.75:
            return "strong"
        if score >= 0.50:
            return "medium"
        return "weak"

    # No high-specificity tokens — identifiers and paths may contribute to strong
    for ttype in ("path", "identifier", "port", "model", "config", "quoted"):
        if _token_in_content(ttype, qt, content_lower):
            return "strong"

    score = hit.get("score", 0)
    # Score fallback: only for non-empty queries. Empty query has no tokens to
    # match and should not claim strong evidence based on score alone.
    if q and score >= 0.75:
        return "strong"
    if score >= 0.50:
        return "medium"
    return "weak"


# Exported for test use
def _score_is_strong(score: float) -> bool:
    return score >= 0.75


def _score_is_medium(score: float) -> bool:
    return score >= 0.50


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
            "dynamics_potentiations": 0,
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

        # L3: full hybrid search — ONLY if L2 found nothing strong/medium
        # and always_run_l3 is False (default). This saves latency + tokens.
        l2_has_signal = any(
            _classify_evidence(query, h) in ("strong", "medium")
            for h in l2_hits
        )
        if not l2_has_signal or self._config.always_run_l3:
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

        # Sort: score primary, recency tiebreaker. When prioritize_recent_days
        # is set, hits with created_at within the window get a small additive
        # score boost so fresh memories win ties with ancient ones.
        #
        # When the plugin's MemPalaceAPI has connection (hall/tunnel) dynamics
        # available, also look up the live strength of the connection from the
        # active wing → hit's wing and add a smaller proportional boost. This
        # is the "Hebbian" path: connections the user has been actively
        # reinforcing surface more strongly. Failures here are silent — we
        # never block the recall on a connection lookup.
        recency_window = self._config.prioritize_recent_days
        if recency_window > 0 or self._connection_strength_boost_available():
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc)

            def _recency_boost(h: Dict[str, Any]) -> float:
                date_str = h.get("date") or h.get("created_at") or ""
                if not date_str:
                    return 0.0
                raw = str(date_str)[:10]
                try:
                    dt = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                except Exception:
                    return 0.0
                age_days = (now - dt).days
                if age_days < 0 or age_days > recency_window:
                    return 0.0
                # Linear ramp: 0.10 boost at age=0, 0.0 at age=window
                return 0.10 * (1.0 - age_days / recency_window)

            # Pre-compute connection boosts once (lookup is O(halls+tunnels))
            conn_boosts = self._compute_connection_boosts(deduped) if self._connection_strength_boost_available() else {}

            def _combined_boost(h: Dict[str, Any]) -> float:
                recency = _recency_boost(h) if recency_window > 0 else 0.0
                # Normalize connection boost: scale 0.05..5.0 strength → 0..0.05 additive.
                # Keeps connection effects smaller than recency so age still matters.
                key = (h.get("wing") or "", h.get("room") or "")
                cb = conn_boosts.get(key, 0.0)
                return recency + min(0.05, cb / 100.0)

            deduped.sort(
                key=lambda h: (h.get("score", 0) + _combined_boost(h), str(h.get("date") or "")),
                reverse=True,
            )
        else:
            deduped.sort(key=lambda h: h.get("score", 0), reverse=True)

        # Format with char budget
        recall_block = self._format_recall_block(query, deduped, l0_text)

        self._diag["recall_chars_injected"] = len(recall_block)

        if recall_block:
            self._cache.set(key, recall_block)

        # Hebbian reinforcement: every successful recall strengthens the
        # connections that surfaced the hits. Best-effort — any failure
        # is logged and skipped, never raised (we never break the recall
        # path on a connection write). Skip if all hits were weak-omitted
        # (nothing meaningful was surfaced).
        if recall_block and deduped:
            try:
                updated = self.potentiate_used_connections(deduped)
                if updated:
                    self._diag["dynamics_potentiations"] = (
                        self._diag.get("dynamics_potentiations", 0) + updated
                    )
            except Exception as e:
                logger.debug("[MemPalaceRetrieval] dynamics reinforcement failed: %s", e)

        return recall_block

    # ----------------------------------------------------------------
    # L0 — tiny wake context
    # ----------------------------------------------------------------

    def _run_l0_wake_block(self) -> str:
        """Get the minimal memory stack wake block if available.

        Uses wake_up_context(wing, char_budget) — the correct API method.
        Falls back to the older 'wake_up' attribute name if present (compat).
        """
        try:
            # Primary: wake_up_context(wing, char_budget) — the real API
            ctx_fn = getattr(self._api, "wake_up_context", None)
            if callable(ctx_fn):
                cap = self._config.max_wake_block_chars
                wing = self._config.wake_up_wing or self._config.target_wing or ""
                result = _run_with_timeout(
                    lambda: ctx_fn(wing=wing, char_budget=cap),
                    timeout_seconds=1.0,
                )
                if result and isinstance(result, str):
                    if result:
                        self._diag["l0_wake_hits"] += 1
                    return result[:cap] if len(result) > cap else result
                return ""
            # Fallback: older 'wake_up' attribute (no-arg, if ever exposed)
            wake_fn = getattr(self._api, "wake_up", None)
            if callable(wake_fn):
                result = _run_with_timeout(wake_fn, timeout_seconds=1.0)
                if not result:
                    return ""
                text = result if isinstance(result, str) else str(result)
                cap = self._config.max_wake_block_chars
                if len(text) > cap:
                    text = text[:cap]
                if text:
                    self._diag["l0_wake_hits"] += 1
                return text
            return ""
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

    def _score_floor_for(self, query: str) -> float:
        """Pick the right min_score floor for this query.

        Vague NL queries (no high-specificity tokens) need a relaxed floor
        so the safety-net has hits to work with. Token-rich queries keep
        the strict floor because false positives hurt more.
        """
        strict = self._config.min_score
        relaxed = max(0.25, strict - 0.2)
        if strict <= relaxed:
            return strict
        qt = _extract_query_tokens(query)
        HIGH_SPECIFICITY_TYPES = ("path", "port", "model", "config", "quoted")
        if any(qt.get(tt, set()) for tt in HIGH_SPECIFICITY_TYPES):
            return strict
        return relaxed

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

        # Per-query min_score: relaxed floor for vague NL so the safety net
        # can promote the best medium hit instead of returning zero hits.
        floor = self._score_floor_for(query)

        # L2.5: exact-match step — run BEFORE semantic search when query
        # contains concrete lexical tokens (paths, identifiers, ports).
        # Uses the same search() but with a token-derived exact query to
        # bias toward lexical matches. This runs even without a known wing.
        exact_hits = self._run_l2_exact_match(query, use_wing, use_room)
        hits.extend(exact_hits)

        # KG lookup if enabled (use_kg is the new flag; include_kg_facts is the legacy flag)
        if self._config.use_kg or self._config.include_kg_facts:
            kg_hits = self._run_kg_lookup(query)
            hits.extend(kg_hits)

        # Tunnel following if enabled
        if self._config.follow_tunnels and use_wing:
            tunnel_hits = self._run_tunnel_follow(use_wing, use_room)
            hits.extend(tunnel_hits)

        # Scoped semantic search (if wing is known)
        if use_wing:
            scoped_hits = self._search(
                query,
                wing=use_wing,
                room=use_room or None,
                limit=min(self._config.max_results, 5),
                timeout_ms=min(self._config.max_l3_search_time_ms, 300),
                min_score=floor,
            )
            hits.extend(scoped_hits)

        return hits

    def _run_l2_exact_match(
        self, query: str, wing: str, room: str
    ) -> List[Dict[str, Any]]:
        """L2.5 — exact-match step for concrete lexical tokens.

        Detects paths, identifiers, ports, and other specific tokens in the query.
        Runs a targeted search using the most specific token available, with a very
        short timeout and strict result cap. This biases toward exact hits before
        the broader semantic L2 search.

        Falls back gracefully if no specific tokens are found or if the search
        API is unavailable.
        """
        tokens = _extract_query_tokens(query)

        # Build an exact-match query from the most specific token available
        exact_query: Optional[str] = None
        if tokens.get("path"):
            # Use the shortest path token (most specific) — prefer absolute paths
            paths = sorted(tokens["path"], key=len, reverse=True)
            exact_query = paths[0]
        elif tokens.get("identifier"):
            idents = sorted(tokens["identifier"], key=len, reverse=True)
            exact_query = idents[0]
        elif tokens.get("port"):
            exact_query = tokens["port"].copy().pop() if tokens["port"] else None

        if not exact_query:
            return []

        try:
            return self._search(
                exact_query,
                wing=wing or None,
                room=room or None,
                limit=2,  # very tight cap for exact step
                timeout_ms=min(self._config.max_l3_search_time_ms, 150),
            )
        except Exception:
            # Fail open — missing exact match is not fatal
            return []

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
            min_score=self._score_floor_for(query),
        )

    def _search(
        self,
        query: str,
        wing: str = "",
        room: Optional[str] = None,
        limit: int = 8,
        timeout_ms: int = 400,
        min_score: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Run API search with hard timeout.

        ``min_score`` defaults to the config value. Pass a smaller value
        (e.g. 0.3) when the caller has no high-specificity tokens and we
        want to give the safety-net logic something to work with.
        """
        timeout_s = max(0.05, timeout_ms / 1000.0)

        def _call():
            return self._api.search(
                query,
                wing=wing or None,
                room=room or None,
                limit=limit,
                min_score=min_score if min_score is not None else self._config.min_score,
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
          - weak hits omitted from block by default
          - SAFETY NET: if the query has NO high-specificity tokens and no
            strong/medium hits were found, force-include the best available
            medium-classified hit (without the [medium] prefix downgrade) so
            vague natural-language asks aren't completely blind. The
            classification still applies to all other hits.
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

        # Pre-classify hits and determine if safety net should apply.
        # Safety net: vague NL queries (no high-spec tokens) that have NO
        # strong/medium hits must still surface at least the best medium hit,
        # otherwise the model gets no context at all.
        classified: List[tuple] = []  # (evidence, hit) — preserves order
        for hit in hits:
            evidence = _classify_evidence(query, hit)
            classified.append((evidence, hit))

        has_strong_or_medium = any(ev in ("strong", "medium") for ev, _ in classified)
        query_tokens = _extract_query_tokens(query)
        HIGH_SPECIFICITY_TYPES = ("path", "port", "model", "config", "quoted")
        query_has_high_spec = any(query_tokens.get(tt, set()) for tt in HIGH_SPECIFICITY_TYPES)
        apply_safety_net = (not has_strong_or_medium) and (not query_has_high_spec) and bool(classified)

        if apply_safety_net:
            # Promote the best medium hit (or weakest non-weakest) to strong
            # for this one query — it's our only signal, treat it as strong
            # so the model trusts it.
            best_idx = 0
            best_score = -1.0
            for i, (ev, h) in enumerate(classified):
                s = h.get("score", 0)
                if s > best_score:
                    best_score = s
                    best_idx = i
            ev0, h0 = classified[best_idx]
            classified[best_idx] = ("strong", h0)

        # Insert KG header before first KG-sourced hit (backward compat)
        kg_started = False
        for evidence, hit in classified:
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
                "min_score": self._config.min_score,
                "prioritize_recent_days": self._config.prioritize_recent_days,
                "follow_tunnels": self._config.follow_tunnels,
                "max_tunnel_hops": self._config.max_tunnel_hops,
                "max_tunnel_hits": self._config.max_tunnel_hits,
                "use_kg": self._config.use_kg,
                "prefer_active_project": self._config.prefer_active_project,
                "memory_stack_enabled": self._config.memory_stack_enabled,
                "dynamics_enabled": self._config.dynamics_enabled,
            },
        }

    # ----------------------------------------------------------------
    # Connection-strength boost (Hebbian path via MemPalace dynamics)
    # ----------------------------------------------------------------

    def _connection_strength_boost_available(self) -> bool:
        """True iff the underlying api has halls/tunnels persistence loaded
        and the config flag is on. Fail-closed: any missing piece returns False.
        """
        if not getattr(self._config, "dynamics_enabled", True):
            return False
        api = self._api
        if api is None:
            return False
        if not (getattr(api, "_load_tunnels_fn", None) or getattr(api, "_load_halls_fn", None)):
            return False
        return True

    def _compute_connection_boosts(
        self, hits: List[Dict[str, Any]]
    ) -> Dict[tuple, float]:
        """Return a map of (wing, room) → connection strength for each hit.

        Looks at halls (in same wing as the hit) and tunnels (touched the
        hit's wing). The maximum strength wins, so a strong tunnel always
        promotes the hit regardless of any weaker halls also touching the
        wing. The active wing (if known) is preferred for tunnel matching;
        we boost the tunnel that connects active → hit.wing.
        """
        if not hits:
            return {}
        api = self._api
        try:
            api._ensure_imported()
        except Exception:
            return {}
        active_wing = (
            self._config.wake_up_wing
            or self._config.target_wing
            or ""
        )
        # Collect candidate wings from the hits.
        target_wings = {h.get("wing") for h in hits if h.get("wing")}
        if not target_wings:
            return {}

        out: Dict[tuple, float] = {}
        # --- Tunnels ---
        load_tunnels = getattr(api, "_load_tunnels_fn", None)
        if load_tunnels:
            try:
                tunnels = list(load_tunnels() or [])
            except Exception as e:
                logger.debug("[MemPalaceRetrieval] load_tunnels failed: %s", e)
                tunnels = []
            for t in tunnels:
                src = (
                    (t.get("source") or {}).get("wing")
                    if isinstance(t.get("source"), dict)
                    else t.get("source_wing")
                )
                tgt = (
                    (t.get("target") or {}).get("wing")
                    if isinstance(t.get("target"), dict)
                    else t.get("target_wing")
                )
                # Boost when the tunnel touches the active wing AND a hit wing
                if active_wing and (active_wing in (src, tgt)):
                    for hit_wing in target_wings:
                        if hit_wing in (src, tgt) and hit_wing != active_wing:
                            key = (hit_wing, "")
                            cur = out.get(key, 0.0)
                            strength = float(t.get("strength", 0.0))
                            if strength > cur:
                                out[key] = strength
                # Also boost any hit wing connected to ANY other hit wing
                elif not active_wing and src and tgt and src in target_wings and tgt in target_wings:
                    for hit_wing in (src, tgt):
                        key = (hit_wing, "")
                        cur = out.get(key, 0.0)
                        strength = float(t.get("strength", 0.0))
                        if strength > cur:
                            out[key] = strength

        # --- Halls (single-wing: contribute to all rooms in that wing) ---
        load_halls = getattr(api, "_load_halls_fn", None)
        if load_halls:
            try:
                halls = list(load_halls() or [])
            except Exception as e:
                logger.debug("[MemPalaceRetrieval] load_halls failed: %s", e)
                halls = []
            for h in halls:
                h_wing = h.get("wing")
                if h_wing not in target_wings:
                    continue
                # Hall strength boosts the (wing, "") aggregate. Rooms in
                # the same wing get the same boost (we don't track per-room
                # entity pairs here — that's a future enhancement).
                key = (h_wing, "")
                cur = out.get(key, 0.0)
                strength = float(h.get("strength", 0.0))
                if strength > cur:
                    out[key] = strength

        # Also allow room-level keys to inherit the wing-level boost.
        for hit in hits:
            wing = hit.get("wing") or ""
            room = hit.get("room") or ""
            if not wing:
                continue
            wing_key = (wing, "")
            if wing_key in out:
                out[(wing, room)] = max(out.get((wing, room), 0.0), out[wing_key])
        return out

    def potentiate_used_connections(self, hits: List[Dict[str, Any]]) -> int:
        """Hebbian reinforcement: every hit the user just saw strengthens
        the connection that surfaced it. Called after a successful prefetch
        (cache hit or fresh fetch) so that frequently-accessed connections
        naturally rise to the top.

        Returns the count of connections that were actually updated. Best-effort:
        any failure is logged and skipped, never raised.
        """
        if not getattr(self._config, "dynamics_enabled", True):
            return 0
        if not hits:
            return 0
        api = self._api
        if api is None or getattr(api, "potentiate", None) is None:
            return 0
        active_wing = (
            self._config.wake_up_wing
            or self._config.target_wing
            or ""
        )
        # Collect unique (wing, room) keys touched
        keys: Set[tuple] = set()
        for h in hits:
            wing = h.get("wing") or ""
            room = h.get("room") or ""
            if wing:
                keys.add((wing, room))
        # Also include the active wing's outgoing tunnels for any hit wing
        if active_wing:
            for h in hits:
                if h.get("wing") and h["wing"] != active_wing:
                    keys.add((active_wing, h["wing"]))
        # Look up the relevant connection IDs from the loaded stores.
        load_tunnels = getattr(api, "_load_tunnels_fn", None)
        load_halls = getattr(api, "_load_halls_fn", None)
        updated = 0
        # Tunnel IDs that touch a hit wing
        if load_tunnels:
            try:
                tunnels = list(load_tunnels() or [])
            except Exception:
                tunnels = []
            hit_wings = {h.get("wing") for h in hits if h.get("wing")}
            for t in tunnels:
                src = (
                    (t.get("source") or {}).get("wing")
                    if isinstance(t.get("source"), dict)
                    else t.get("source_wing")
                )
                tgt = (
                    (t.get("target") or {}).get("wing")
                    if isinstance(t.get("target"), dict)
                    else t.get("target_wing")
                )
                if active_wing and (active_wing in (src, tgt)) and any(
                    w in (src, tgt) and w != active_wing for w in hit_wings
                ):
                    try:
                        r = api.potentiate(t["id"], "tunnel", 0.05)
                        if r.get("success"):
                            updated += 1
                    except Exception as e:
                        logger.debug("[MemPalaceRetrieval] potentiate tunnel %s failed: %s", t.get("id"), e)
        # Hall IDs for hit wings
        if load_halls:
            try:
                halls = list(load_halls() or [])
            except Exception:
                halls = []
            hit_wings = {h.get("wing") for h in hits if h.get("wing")}
            for h in halls:
                if h.get("wing") in hit_wings:
                    try:
                        r = api.potentiate(h["id"], "hall", 0.05)
                        if r.get("success"):
                            updated += 1
                    except Exception as e:
                        logger.debug("[MemPalaceRetrieval] potentiate hall %s failed: %s", h.get("id"), e)
        return updated

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