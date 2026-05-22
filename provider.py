"""MemPalaceMemoryProvider — Hermes MemoryProvider lifecycle adapter.

Thin adapter: forwards to MemPalace API (search, storage, KG, graph, diary).
Does not implement a second memory system. Fail-open throughout.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from .config import MemPalaceConfig, load_config
from .api import MemPalaceAPI
from .facts import SchemaValidatedFactExtractor
from .retrieval import MemPalaceRetrieval

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------
# HolographicMirror — real implementation or explicitly unavailable
# ----------------------------------------------------------------


class HolographicMirror:
    """Holographic structured fact mirror.

    Available when mempalace.holographic is importable.
    Unavailable otherwise — diagnostics reports hologram_available=False,
    never a silent success/no-op.
    """

    _available: Optional[bool] = None

    def __init__(self, config: MemPalaceConfig, api: MemPalaceAPI):
        self._config = config
        self._api = api
        self._holo = None
        self._check_available()

    def _check_available(self) -> None:
        if HolographicMirror._available is not None:
            return
        try:
            from mempalace.holographic import HolographicMemoryStore
            HolographicMirror._available = True
        except Exception:
            HolographicMirror._available = False

    @property
    def available(self) -> bool:
        return HolographicMirror._available is True

    def ensure_enabled(self) -> bool:
        """Try to init holographic. Return False if unavailable or errored."""
        if not self._config.holographic_enabled:
            return False
        if not self.available:
            logger.warning("[MemPalace] Holographic requested but unavailable")
            return False
        if self._holo is not None:
            return True
        try:
            from mempalace.holographic import HolographicMemoryStore
            db_path = None
            if self._api._palace_data_dir:
                db_path = self._api._palace_data_dir
            self._holo = HolographicMemoryStore(db_path=db_path)
            return True
        except Exception as e:
            logger.warning("[MemPalace] Holographic init failed: %s", e)
            return False

    def add_fact(self, fact: Dict[str, Any]) -> None:
        if not self._holo:
            return
        try:
            self._holo.add_fact(
                subject=fact.get("subject", ""),
                predicate=fact.get("predicate", ""),
                object_=fact.get("object_", ""),
                confidence=fact.get("confidence", 0.8),
            )
        except Exception as e:
            logger.debug("[MemPalace] holographic add_fact failed: %s", e)

    def search_facts(self, query: str, limit: int = 3) -> List[Dict[str, Any]]:
        if not self._holo:
            return []
        try:
            return self._holo.search(query, limit=limit) or []
        except Exception as e:
            logger.debug("[MemPalace] holographic search failed: %s", e)
            return []


# ----------------------------------------------------------------
# MemPalaceMemoryProvider
# ----------------------------------------------------------------


class MemPalaceMemoryProvider:
    """MemPalace-backed Hermes MemoryProvider.

    Thin lifecycle adapter. All real work delegates to:
    - MemPalaceAPI (storage, search, KG, graph, diary)
    - MemPalaceRetrieval (bounded retrieval with cache TTL)
    - HolographicMirror (structured fact mirror, optional)

    Fail-open throughout: errors are logged but never propagate
    to Hermes chat.
    """

    def __init__(self, config: Optional[MemPalaceConfig] = None):
        self._config = config or load_config()
        self._session_id: str = ""
        self._turn_count: int = 0
        self._initialized: bool = False
        self._prefetch_cache: Dict[Any, str] = {}
        self._prefetch_inflight: Dict[Any, threading.Event] = {}
        self._prefetch_lock = threading.RLock()
        self._wake_block: str = ""
        self._wake_prefetch_applied: bool = False
        self._diary_context: Optional[Dict[str, Any]] = None

        # Thread tracking
        self._threads: Dict[str, threading.Thread] = {}
        self._threads_lock = threading.Lock()

        # Metrics
        self._metrics: Dict[str, int] = {
            "prefetch_cache_hits": 0,
            "prefetch_cache_misses": 0,
            "prefetch_cache_evictions": 0,
            "prefetch_timeouts": 0,
            "ingest_attempts": 0,
            "ingest_errors": 0,
        }

        # Components (lazy init)
        self._mp_api: Optional[MemPalaceAPI] = None
        self._retrieval: Optional[MemPalaceRetrieval] = None
        self._holo_mirror: Optional[HolographicMirror] = None

    # ----------------------------------------------------------------
    # Availability
    # ----------------------------------------------------------------

    def is_available(self) -> bool:
        if not self._config.enabled:
            return False
        if self._mp_api is not None:
            return bool(getattr(self._mp_api, "is_available", False)())
        # Pre-check palace path exists
        from pathlib import Path
        if not self._config.palace_data_dir or not Path(self._config.palace_data_dir).exists():
            return False
        return True

    # ----------------------------------------------------------------
    # Lazy init — called before first use
    # ----------------------------------------------------------------

    def _ensure_api(self) -> None:
        if self._mp_api is not None:
            return
        self._mp_api = MemPalaceAPI(
            palace_data_dir=self._config.palace_data_dir,
            mempalace_lib_dir=self._config.mempalace_lib_dir,
            config=self._config,
        )
        if self._config.retrieval_enabled:
            self._retrieval = MemPalaceRetrieval(self._mp_api, self._config)
        if self._config.holographic_enabled:
            self._holo_mirror = HolographicMirror(self._config, self._mp_api)
            self._holo_mirror.ensure_enabled()

    # ----------------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------------

    def _metric(self, name: str) -> None:
        self._metrics[name] = self._metrics.get(name, 0) + 1

    def _turn_source_file(self, *, session_id: str = "", content: str = "") -> str:
        sid = (session_id or self._session_id or "session")[:16]
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:10] if content else "nohash"
        return f"session_{sid}_turn_{self._turn_count}_{digest}"

    def _start_tracked_thread(self, name: str, fn) -> None:
        with self._threads_lock:
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self._threads[name] = t

    def _join_background_threads(self) -> None:
        timeout = self._config.thread_join_timeout_ms / 1000.0
        with self._threads_lock:
            for name, t in list(self._threads.items()):
                remaining = max(0.1, timeout)
                start = time.monotonic()
                t.join(timeout=remaining)
                elapsed = time.monotonic() - start
                timeout = max(0, timeout - elapsed)
                if not t.is_alive():
                    del self._threads[name]

    # ----------------------------------------------------------------
    # Prefetch key
    # ----------------------------------------------------------------

    def _prefetch_key(
        self, query: str, session_id: str, wing: str = "", room: str = ""
    ) -> Any:
        return (session_id, query, wing, room)

    def _cache_prefetch_result(self, key: Any, result: str) -> None:
        with self._prefetch_lock:
            if len(self._prefetch_cache) >= self._config.prefetch_cache_size:
                oldest = min(self._prefetch_cache, key=lambda k: self._prefetch_cache[k])
                del self._prefetch_cache[oldest]
                self._metric("prefetch_cache_evictions")
            self._prefetch_cache[key] = result

    # ----------------------------------------------------------------
    # MemoryProvider ABC
    # ----------------------------------------------------------------

    def initialize(self, session_id: str = "", **kwargs) -> None:
        """Warm up MemPalace API on session start."""
        self._session_id = session_id or self._session_id or ""
        self._turn_count = 0
        self._initialized = True
        self._ensure_api()

        if self._config.wake_up_on_session_start:
            self._load_wake_block_if_needed(force=True)

        # Diary read on session start
        if self._config.diary_enabled and self._config.diary_read_on_start and self._mp_api:
            agent = self._config.diary_agent_name or self._config.agent_name
            try:
                entries = self._mp_api.diary_read(agent, last_n=self._config.diary_last_n, wing=self._config.diary_wing)
                if isinstance(entries, dict) and entries.get("entries"):
                    self._diary_context = entries
                    logger.info("[MemPalace] loaded %d diary entries for %s", len(entries["entries"]), agent)
            except Exception as e:
                logger.debug("[MemPalace] diary read on start failed: %s", e)

    def prefetch(
        self,
        query: str,
        session_id: str = "",
        **kwargs,
    ) -> str:
        """Prefetch MemPalace context with bounded timeout.

        Uses retrieval engine with cache TTL and stale fallback.
        Never blocks Hermes chat.
        """
        if not self._config.enabled or not self._initialized:
            return ""
        self._ensure_api()

        # Memory stack L0/L1 wake block
        if self._wake_block and not self._wake_prefetch_applied:
            return self._wake_block

        wing = str(kwargs.get("prefetch_wing") or kwargs.get("wing") or "")
        room = str(kwargs.get("prefetch_room") or kwargs.get("room") or "")

        # Try cache
        key = self._prefetch_key(query, session_id or self._session_id, wing, room)
        with self._prefetch_lock:
            if key in self._prefetch_cache:
                self._metric("prefetch_cache_hits")
                return self._prefetch_cache[key]
            self._metric("prefetch_cache_misses")

        # Queue prefetch if background retrieval enabled
        if self._config.background_retrieval and self._retrieval:
            self._retrieval.queue_prefetch(
                query, session_id or self._session_id,
                prefetch_wing=wing, prefetch_room=room,
            )

        # Inline retrieval with timeout
        if self._retrieval:
            result = self._retrieval.prefetch(
                query, session_id or self._session_id,
                prefetch_wing=wing, prefetch_room=room,
                background=False,
            )
            if result:
                self._cache_prefetch_result(key, result)
                return result

        # Fallback: direct search (synchronous, bounded)
        try:
            results = self._mp_api.search(
                query, wing=wing, room=room,
                limit=self._config.max_results, min_score=self._config.min_score,
            )
            if results:
                combined = "\n".join(r.get("content", "")[:500] for r in results)
                self._cache_prefetch_result(key, combined)
                return combined[:2000]
        except Exception as e:
            logger.debug("[MemPalace] prefetch error: %s", e)

        return ""

    def queue_prefetch(
        self,
        query: str,
        session_id: str = "",
        **kwargs,
    ) -> None:
        """Queue a prefetch to run in background. No-op if background_retrieval=false."""
        if not self._config.enabled or not self._initialized:
            return
        if not self._config.background_retrieval:
            return
        self._ensure_api()
        if self._retrieval:
            wing = str(kwargs.get("prefetch_wing") or "")
            room = str(kwargs.get("prefetch_room") or "")
            self._retrieval.queue_prefetch(query, session_id or self._session_id, prefetch_wing=wing, prefetch_room=room)

    def sync_turn(
        self,
        role: str,
        content: str,
        session_id: str = "",
        **kwargs,
    ) -> None:
        """Ingest a conversation turn into MemPalace.

        Controlled by ingestion.mode (each_turn|session_end|none).
        All write paths use duplicate checks.
        """
        if not self._config.enabled:
            return
        if self._config.ingestion_mode not in ("each_turn",):
            return
        if not content or len(content) < self._config.min_turn_length:
            return

        self._ensure_api()
        self._turn_count += 1

        # Truncate to max_turn_length before chunking
        content = content[: self._config.max_turn_length]

        def _ingest():
            self._metric("ingest_attempts")
            try:
                combined = f"{role}: {content}"
                agent = self._config.agent_name or "hermes"
                wing = self._config.target_wing
                room = self._config.target_room
                source = self._turn_source_file(session_id=session_id or self._session_id, content=content)

                # Chunk and add with duplicate check on each chunk
                self._mp_api.chunk_and_add(
                    content=combined,
                    source_file=source,
                    wing=wing,
                    room=room,
                    agent=agent,
                )

                # AAAK digest compression (optional, off by default)
                if self._config.aaak_enabled and self._config.aaak_compress_digests and len(combined) > 100:
                    compressed = self._mp_api.dialect_compress(
                        combined,
                        metadata={"wing": wing, "room": room},
                    )
                    if compressed:
                        self._mp_api.add_drawer(
                            content=compressed,
                            source_file=source + "_aaak",
                            wing=wing,
                            room="compressed",
                            agent=agent,
                        )

                # Fact extraction (optional, off by default)
                if self._config.extract_facts_each_turn and self._config.fact_extraction_mode in ("schema", "regex"):
                    facts = SchemaValidatedFactExtractor.extract_facts(
                        combined,
                        max_facts=self._config.max_facts_per_turn,
                        min_confidence=self._config.min_confidence,
                        mode=self._config.fact_extraction_mode,
                        allowed_predicates=self._config.allowed_predicates or None,
                    )
                    for fact in facts:
                        self._mp_api.kg_add_triple(
                            fact["subject"], fact["predicate"], fact["object_"],
                            confidence=fact.get("confidence", 0.8),
                            valid_from=fact.get("valid_from", ""),
                        )
                        if self._holo_mirror and self._holo_mirror.available:
                            self._holo_mirror.add_fact(fact)

            except Exception as e:
                self._metric("ingest_errors")
                logger.warning("[MemPalace] sync_turn failed: %s", e)

        if self._config.background_ingest:
            self._start_tracked_thread("ingest", _ingest)
        else:
            _ingest()

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Mirror Hermes built-in memory writes to MemPalace + Holographic."""
        if not self._config.enabled:
            return
        self._ensure_api()

        # Memory mirror
        if self._config.memory_mirror_enabled:
            try:
                wing = self._config.mirror_target_wing
                room = "conversations"
                agent = self._config.agent_name or "hermes"

                if action == "add" and self._config.mirror_add:
                    self._mp_api.add_drawer(
                        content=content, wing=wing, room=room, agent=agent,
                    )
                elif action == "replace" and self._config.mirror_replace:
                    self._mp_api.add_drawer(
                        content=content, wing=wing, room=room, agent=agent,
                    )
                elif action == "remove" and self._config.mirror_remove:
                    # Only invalidate KG if concrete triple provided
                    meta = metadata or {}
                    triple = meta.get("kg_triple") or meta.get("triple")
                    if isinstance(triple, dict):
                        self._mp_api.kg_invalidate_triple(
                            triple.get("subject", ""),
                            triple.get("predicate", ""),
                            triple.get("object", ""),
                            ended=triple.get("ended"),
                        )
            except Exception as e:
                logger.debug("[MemPalace] on_memory_write failed: %s", e)

    def on_pre_compress(self, messages: list) -> str:
        """Extract facts from messages about to be compressed."""
        if not self._config.enabled or not self._initialized:
            return ""
        if not self._config.extract_facts_each_turn:
            return ""

        combined = " ".join(m.get("content", "") for m in messages if isinstance(m, dict))[
            : self._config.max_turn_length
        ]
        if len(combined) < self._config.min_turn_length:
            return ""

        facts = SchemaValidatedFactExtractor.extract_facts(
            combined,
            max_facts=self._config.max_facts_per_turn,
            min_confidence=self._config.min_confidence,
            mode=self._config.fact_extraction_mode,
            allowed_predicates=self._config.allowed_predicates or None,
        )
        if not facts:
            return ""

        lines = [
            f"- {f['subject']} {f['predicate']} {f['object_']} (conf={f['confidence']:.2f})"
            for f in facts
        ]
        return "Extracted facts from compressed context:\n" + "\n".join(lines)

    def on_delegation(
        self, task: str, result: str, *, child_session_id: str = "", **kwargs
    ) -> None:
        """Ingest subagent delegation results into MemPalace."""
        if not self._config.enabled:
            return
        if self._config.ingestion_mode == "none":
            return

        self._ensure_api()
        combined = f"Delegated task: {task}\nResult: {result}"[: self._config.max_turn_length]

        def _ingest():
            self._metric("ingest_attempts")
            try:
                self._mp_api.chunk_and_add(
                    content=combined,
                    source_file=f"delegation_{child_session_id}",
                    wing=self._config.target_wing,
                    room=self._config.target_room,
                    agent=self._config.agent_name or "hermes",
                )
            except Exception as e:
                self._metric("ingest_errors")
                logger.debug("[MemPalace] on_delegation failed: %s", e)

        if self._config.background_ingest:
            self._start_tracked_thread("delegation-ingest", _ingest)
        else:
            _ingest()

    def on_session_start(self, session_id: str, **kwargs) -> None:
        """Handle session start. Initialize API, load wake block, read diary."""
        self.initialize(session_id=session_id, **kwargs)

    def on_session_end(self, messages: list) -> None:
        """Handle session end. Write diary entry. Do NOT launch session importer."""
        if not self._config.enabled:
            return

        # Diary write
        if self._config.diary_enabled and self._mp_api:
            agent = self._config.diary_agent_name or self._config.agent_name
            summary = self._build_session_summary(messages)
            if summary:

                def _write_diary():
                    try:
                        self._mp_api.diary_write(
                            agent, summary,
                            topic=self._config.diary_topic,
                            wing=self._config.diary_wing,
                        )
                    except Exception as e:
                        logger.debug("[MemPalace] diary write failed: %s", e)

                if self._config.background_ingest:
                    self._start_tracked_thread("diary-write", _write_diary)
                else:
                    _write_diary()

        # Join background threads
        self._join_background_threads()

    def _build_session_summary(self, messages: list) -> str:
        """Build a short summary of the session for diary write."""
        if not messages:
            return ""
        recent = messages[-6:] if len(messages) > 6 else messages
        parts = []
        for m in recent:
            if not isinstance(m, dict):
                continue
            role = m.get("role", "")
            content = m.get("content", "")
            if not content:
                continue
            snippet = content[:200]
            parts.append(f"{role}: {snippet}")
        if not parts:
            return ""
        summary = "\n".join(parts)
        if len(summary) > 1000:
            summary = summary[:1000] + "..."
        return summary

    # ----------------------------------------------------------------
    # Wake block (L0-L1 memory stack)
    # ----------------------------------------------------------------

    def _load_wake_block_if_needed(self, force: bool = False) -> None:
        if self._wake_block and not force:
            return
        if not self._mp_api:
            self._ensure_api()
        try:
            self._wake_block = self._mp_api.wake_up_context(
                wing=self._config.wake_up_wing or "",
                char_budget=self._config.wake_char_budget,
            )
        except Exception as e:
            logger.debug("[MemPalace] wake_up_context failed: %s", e)
            self._wake_block = ""

    def _reset_memory_stack_session_state(self) -> None:
        self._wake_block = ""
        self._wake_prefetch_applied = False

    # ----------------------------------------------------------------
    # System prompt
    # ----------------------------------------------------------------

    def system_prompt_block(self) -> str:
        if not self._config.enabled or not self._initialized:
            return ""
        parts = ["MemPalace memory provider active"]
        if self._config.memory_stack_enabled:
            parts.append("memory stack L0-L3")
        if self._config.extract_facts_each_turn:
            parts.append("fact extraction")
        if self._config.holographic_enabled:
            parts.append("holographic mirror")
        if self._config.graph_enabled:
            parts.append("graph-assisted prefetch")
        if self._config.diary_enabled:
            parts.append("agent diary")
        if self._config.aaak_enabled:
            parts.append("AAAK compression")
        return " | ".join(parts)

    # ----------------------------------------------------------------
    # Config schema
    # ----------------------------------------------------------------

    def get_config_schema(self) -> List[Dict[str, Any]]:
        # Schema is static — available even before API is initialized
        if self._mp_api is not None:
            return self._mp_api.get_config_schema()
        # Eagerly create API just for schema
        tmp_api = MemPalaceAPI(
            palace_data_dir=self._config.palace_data_dir,
            mempalace_lib_dir=self._config.mempalace_lib_dir,
            config=self._config,
        )
        return tmp_api.get_config_schema()

    # ----------------------------------------------------------------
    # Diagnostics
    # ----------------------------------------------------------------

    def diagnostics(self) -> Dict[str, Any]:
        cache_stats = {}
        retrieval_stats = {}
        if self._retrieval:
            cache_stats = self._retrieval._cache.stats()
            retrieval_stats = self._retrieval.diagnostics().get("retrieval_timeout_seconds", 0)

        return {
            "name": "mempalace",
            "enabled": self._config.enabled,
            "initialized": self._initialized,
            "session_id": self._session_id,
            "prefetch_cache_size": len(self._prefetch_cache),
            "prefetch_cache_limit": self._config.prefetch_cache_size,
            "cache_stats": cache_stats,
            "retrieval_timeout_seconds": retrieval_stats,
            "background_threads": len(self._threads),
            "metrics": dict(self._metrics),
            "hologram_available": (
                self._holo_mirror.available if self._holo_mirror else False
            ),
            "diary_enabled": self._config.diary_enabled,
            "diary_context_loaded": self._diary_context is not None,
            "config": {
                "ingestion_mode": self._config.ingestion_mode,
                "retrieval_enabled": self._config.retrieval_enabled,
                "memory_stack_enabled": self._config.memory_stack_enabled,
                "duplicate_check_enabled": self._config.duplicate_check_enabled,
                "duplicate_threshold": self._config.duplicate_threshold,
            },
        }

    # ----------------------------------------------------------------
    # Shutdown
    # ----------------------------------------------------------------

    def shutdown(self) -> None:
        self._join_background_threads()
        self._prefetch_cache.clear()
        self._prefetch_inflight.clear()

    # ----------------------------------------------------------------
    # Diary context injection
    # ----------------------------------------------------------------

    def get_diary_context(self) -> str:
        """Return bounded diary context for injection into system prompt."""
        if not self._diary_context:
            return ""
        entries = self._diary_context.get("entries", [])
        if not entries:
            return ""
        lines = ["--- Recent diary entries ---"]
        for e in entries:
            date = e.get("date", "?")
            topic = e.get("topic", "?")
            content = e.get("content", "")[:200]
            lines.append(f"[{date}/{topic}] {content}")
        result = "\n".join(lines)
        # Hard cap
        if len(result) > 1500:
            result = result[:1500] + "\n...(truncated)"
        return result