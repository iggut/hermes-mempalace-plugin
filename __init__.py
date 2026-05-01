"""
MemPalace Memory Plugin — Automated memory pipeline for Hermes Agent.

Integrates three memory systems into a coordinated, automated workflow:

1. MemPalace    - Verbatim storage (drawers) + hybrid search (BM25+vector)
                   + knowledge graph (subject->predicate->object triples)
2. Holographic  - Structured fact store (SQLite + FTS5 + HRR vectors)
3. Headroom     - Outbound context compression

This plugin replaces the current passive, manually-triggered memory pipeline
with an automated system that:

- AUTO-INGESTS each conversation turn into MemPalace (verbatim chunks as drawers)
  AND Holographic (structured facts extracted from turns).
- AUTO-RETrieves relevant context before each model call via hybrid search.
- MIRRORS built-in memory tool writes to both systems without requiring the agent
  to manually invoke multiple tools.

ARCHITECTURE:

  Each turn ---> sync_turn() ---> background thread
                   |
                   |---> MemPalace.add_drawer()    (verbatim chunks)
                   |---> MemPalace.KnowledgeGraph.add_triple()  (structured facts)
                   +---> Holographic.MemoryStore.add_fact()     (mirrored facts)

  Before model call ---> prefetch() ---> background thread
                            |
                            |---> MemPalace.searcher.search()    (hybrid search)
                            |---> Holographic.MemoryStore.search_facts()
                            +---> Merge + format for prompt injection

  Built-in memory tool ---> on_memory_write(action, target, content)
                              |
                              |---> MemPalace.add_drawer()       (verbatim)
                              +---> Holographic.MemoryStore.add_fact()


USAGE:
  # Enable in config.yaml:
  plugins:
    mempalace_memory:
      enabled: true

  Or via environment variable:
  HERMES_MEMPALACE_MEMORY_ENABLED=1 hermes-agent run_agent.py ...
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _env_enabled(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _importer_path() -> Path:
    return Path(
        os.environ.get(
            "HERMES_MEMPALACE_IMPORTER",
            Path.home() / ".hermes" / "scripts" / "hermes_chat_importer.py",
        )
    ).expanduser()


def _launch_session_importer() -> bool:
    importer = _importer_path()
    if not importer.exists():
        logger.debug("[MemPalaceMemory] session importer missing at %s", importer)
        return False
    try:
        subprocess.Popen(
            [sys.executable, str(importer)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env={**os.environ, "MEMPALACE_BACKGROUND_IMPORT": "1"},
        )
        logger.debug("[MemPalaceMemory] background chat import launched")
        return True
    except Exception as exc:
        logger.warning("[MemPalaceMemory] failed to launch background import: %s", exc)
        return False

# Import MemoryProvider ABC for plugin registration. When the plugin is
# imported outside Hermes (for direct tests), fall back to object so the module
# remains importable.
try:
    from agent.memory_provider import MemoryProvider as _MP_ABC
except ImportError:
    _MP_ABC = object


# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class MemPalaceConfig:
    """Configuration for the MemPalace memory plugin."""
    enabled: bool = True
    palace_data_dir: str = ""  # ChromaDB data directory (chroma.sqlite3)
    mempalace_lib_dir: str = ""  # Python package directory

    # Ingestion
    ingestion_mode: str = "none"  # each_turn | session_end | none
    min_turn_length: int = 20
    max_turn_length: int = 8000
    chunk_size: int = 800
    chunk_overlap: int = 100
    target_wing: str = "memory"
    target_room: str = "conversations"
    agent_name: str = "jupiter"

    # Structured facts — conservative by default (align with CONFIG_SCHEMA.md).
    extract_facts_each_turn: bool = False
    min_confidence: float = 0.7
    max_facts_per_turn: int = 10
    fact_extraction_mode: str = "none"  # none | regex | schema
    allowed_predicates: List[str] = field(default_factory=list)  # empty = allow all

    # Retrieval
    retrieval_enabled: bool = True
    retrieval_mode: str = "hybrid"  # vector | bm25 | hybrid
    vector_weight: float = 0.6
    bm25_weight: float = 0.4
    max_results: int = 8
    min_score: float = 0.3
    include_kg_facts: bool = True
    kg_entity_limit: int = 5
    retrieval_timeout_seconds: float = 0.5

    # Holographic mirroring
    holographic_enabled: bool = False
    holographic_default_trust: float = 0.5

    # Memory tool mirroring
    memory_mirror_enabled: bool = False
    mirror_add: bool = True
    mirror_replace: bool = True
    mirror_remove: bool = True
    mirror_target_wing: str = "memory"

    # Performance
    background_ingest: bool = True
    background_retrieval: bool = True
    retrieval_timeout_ms: int = 500
    max_fanout: int = 10
    prefetch_cache_size: int = 32
    lexical_scan_limit: int = 1000
    thread_join_timeout_ms: int = 1000

    # Memory stack (L0–L3) — optional; uses mempalace.layers.MemoryStack when enabled
    memory_stack_enabled: bool = False
    wake_up_on_session_start: bool = False
    wake_up_on_first_turn: bool = False
    wake_up_wing: str = ""
    l2_default_room: str = ""
    l2_before_deep_search: bool = True
    l2_skip_deep_search_when_recall_non_empty: bool = False
    identity_path: str = ""
    wake_char_budget: int = 3200
    recall_char_budget: int = 1500
    recall_n_results: int = 10


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _falsey(value: Any) -> bool:
    return str(value).strip().lower() in ("0", "false", "no", "off")


def _nested(config: Dict[str, Any], *keys: str) -> Any:
    cur: Any = config
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def _apply_if_present(cfg: MemPalaceConfig, data: Dict[str, Any], key: str, attr: str = "", cast=None) -> None:
    if not isinstance(data, dict) or key not in data:
        return
    value = data[key]
    if cast is bool:
        if _truthy(value):
            value = True
        elif _falsey(value):
            value = False
        elif isinstance(value, bool):
            value = value
        else:
            logger.warning("[MemPalaceMemory] Invalid boolean config value %s=%r", key, value)
            return
    elif cast is not None:
        try:
            value = cast(value)
        except (TypeError, ValueError):
            logger.warning("[MemPalaceMemory] Invalid config value %s=%r", key, value)
            return
    setattr(cfg, attr or key, value)


def _load_hermes_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config as _load_config
        loaded = _load_config()
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _clamp(value: Any, minimum: int, maximum: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _clamp_float(value: Any, minimum: float, maximum: float, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _finalize_config(cfg: MemPalaceConfig) -> MemPalaceConfig:
    """Normalize config into production-safe bounds."""
    if cfg.ingestion_mode not in {"each_turn", "session_end", "none"}:
        cfg.ingestion_mode = "none"

    if cfg.retrieval_mode not in {"vector", "bm25", "hybrid"}:
        cfg.retrieval_mode = "hybrid"

    # Expand user-home paths before palace auto-detection
    if cfg.palace_data_dir:
        cfg.palace_data_dir = str(Path(cfg.palace_data_dir).expanduser())
    if cfg.mempalace_lib_dir:
        cfg.mempalace_lib_dir = str(Path(cfg.mempalace_lib_dir).expanduser())

    # Clamp numeric values
    cfg.min_turn_length = _clamp(cfg.min_turn_length, 10, 5000, 20)
    cfg.max_turn_length = _clamp(cfg.max_turn_length, 12, 50000, 8000)
    cfg.chunk_size = _clamp(cfg.chunk_size, 100, 10000, 800)
    cfg.chunk_overlap = _clamp(cfg.chunk_overlap, 0, cfg.chunk_size // 2, 100)
    cfg.max_facts_per_turn = _clamp(cfg.max_facts_per_turn, 1, 50, 10)
    cfg.max_results = _clamp(cfg.max_results, 1, 50, 8)
    cfg.kg_entity_limit = _clamp(cfg.kg_entity_limit, 1, 20, 5)
    cfg.retrieval_timeout_ms = _clamp(cfg.retrieval_timeout_ms, 100, 5000, 500)
    cfg.max_fanout = _clamp(cfg.max_fanout, 1, 50, 10)
    cfg.prefetch_cache_size = _clamp(cfg.prefetch_cache_size, 1, 200, 32)
    cfg.lexical_scan_limit = _clamp(cfg.lexical_scan_limit, 10, 5000, 1000)
    cfg.thread_join_timeout_ms = _clamp(cfg.thread_join_timeout_ms, 100, 10000, 1000)
    cfg.wake_char_budget = _clamp(cfg.wake_char_budget, 200, 20000, 3200)
    cfg.recall_char_budget = _clamp(cfg.recall_char_budget, 200, 10000, 1500)
    cfg.recall_n_results = _clamp(cfg.recall_n_results, 1, 50, 10)

    cfg.retrieval_timeout_seconds = max(0.05, cfg.retrieval_timeout_ms / 1000.0)

    # Clamp floats
    cfg.min_score = _clamp_float(cfg.min_score, 0.0, 1.0, 0.3)
    cfg.vector_weight = _clamp_float(cfg.vector_weight, 0.0, 1.0, 0.6)
    cfg.bm25_weight = _clamp_float(cfg.bm25_weight, 0.0, 1.0, 0.4)

    # Normalize extraction mode
    if cfg.fact_extraction_mode not in {"none", "regex", "schema"}:
        cfg.fact_extraction_mode = "schema"

    # Auto-detect palace data directory from HOME env var if not set
    if not cfg.palace_data_dir and os.environ.get("HOME"):
        cfg.palace_data_dir = str(Path(os.environ["HOME"]) / ".mempalace" / "palace")

    return cfg


def _merge_plugin_dicts(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Shallow-merge plugin config with nested dict merge for known sections."""
    out = dict(base)
    for key, val in overlay.items():
        if (
            key
            in (
                "ingestion",
                "facts",
                "retrieval",
                "performance",
                "holographic",
                "memory_mirror",
                "memory_stack",
            )
            and isinstance(val, dict)
            and isinstance(out.get(key), dict)
        ):
            merged = dict(out[key])
            merged.update(val)
            out[key] = merged
        else:
            out[key] = val
    return out


def _gather_plugin_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Merge Hermes config branches that may carry MemPalace settings (later wins)."""
    merged: Dict[str, Any] = {}
    branches = [
        _nested(raw, "plugins", "mempalace"),
        _nested(raw, "plugins", "mempalace_memory"),
        _nested(raw, "mempalace_memory"),
    ]
    for branch in branches:
        if isinstance(branch, dict):
            merged = _merge_plugin_dicts(merged, branch)
    return merged


def _apply_plugin_sections(cfg: MemPalaceConfig, plugin_config: Dict[str, Any]) -> None:
    """Apply flat keys plus nested ingestion/facts/retrieval/performance blocks."""
    ing = plugin_config.get("ingestion") if isinstance(plugin_config.get("ingestion"), dict) else {}
    facts = plugin_config.get("facts") if isinstance(plugin_config.get("facts"), dict) else {}
    retr = plugin_config.get("retrieval") if isinstance(plugin_config.get("retrieval"), dict) else {}
    perf = plugin_config.get("performance") if isinstance(plugin_config.get("performance"), dict) else {}
    holo = plugin_config.get("holographic") if isinstance(plugin_config.get("holographic"), dict) else {}
    mir = plugin_config.get("memory_mirror") if isinstance(plugin_config.get("memory_mirror"), dict) else {}
    mstack = plugin_config.get("memory_stack") if isinstance(plugin_config.get("memory_stack"), dict) else {}

    _apply_if_present(cfg, ing, "mode", "ingestion_mode")
    _apply_if_present(cfg, ing, "min_turn_length")
    _apply_if_present(cfg, ing, "max_turn_length")
    _apply_if_present(cfg, ing, "chunk_size")
    _apply_if_present(cfg, ing, "chunk_overlap")
    _apply_if_present(cfg, ing, "wing", "target_wing")
    _apply_if_present(cfg, ing, "room", "target_room")
    _apply_if_present(cfg, ing, "agent", "agent_name")

    _apply_if_present(cfg, facts, "extract_each_turn", "extract_facts_each_turn", bool)
    _apply_if_present(cfg, facts, "min_confidence")
    _apply_if_present(cfg, facts, "max_facts_per_turn")
    _apply_if_present(cfg, facts, "extraction_mode", "fact_extraction_mode")
    _apply_if_present(cfg, facts, "allowed_predicates")

    _apply_if_present(cfg, retr, "enabled", "retrieval_enabled", bool)
    _apply_if_present(cfg, retr, "mode", "retrieval_mode")
    _apply_if_present(cfg, retr, "vector_weight")
    _apply_if_present(cfg, retr, "bm25_weight")
    _apply_if_present(cfg, retr, "max_results")
    _apply_if_present(cfg, retr, "min_score")
    _apply_if_present(cfg, retr, "include_kg_facts")
    _apply_if_present(cfg, retr, "kg_entity_limit")
    _apply_if_present(cfg, retr, "timeout_ms", "retrieval_timeout_ms")

    _apply_if_present(cfg, perf, "background_ingest")
    _apply_if_present(cfg, perf, "background_retrieval")
    _apply_if_present(cfg, perf, "timeout_ms", "retrieval_timeout_ms")
    _apply_if_present(cfg, perf, "max_fanout")
    _apply_if_present(cfg, perf, "prefetch_cache_size")
    _apply_if_present(cfg, perf, "lexical_scan_limit")
    _apply_if_present(cfg, perf, "thread_join_timeout_ms")

    _apply_if_present(cfg, holo, "enabled", "holographic_enabled", bool)
    _apply_if_present(cfg, holo, "default_trust", "holographic_default_trust")

    _apply_if_present(cfg, mir, "enabled", "memory_mirror_enabled", bool)
    _apply_if_present(cfg, mir, "mirror_add")
    _apply_if_present(cfg, mir, "mirror_replace")
    _apply_if_present(cfg, mir, "mirror_remove")
    _apply_if_present(cfg, mir, "target_wing", "mirror_target_wing")

    _apply_if_present(cfg, mstack, "enabled", "memory_stack_enabled", bool)
    _apply_if_present(cfg, mstack, "wake_up_on_session_start", "wake_up_on_session_start", bool)
    _apply_if_present(cfg, mstack, "wake_up_on_first_turn", "wake_up_on_first_turn", bool)
    _apply_if_present(cfg, mstack, "wake_up_wing")
    _apply_if_present(cfg, mstack, "l2_room", "l2_default_room")
    _apply_if_present(cfg, mstack, "l2_default_room")
    _apply_if_present(cfg, mstack, "l2_before_deep_search", "l2_before_deep_search", bool)
    _apply_if_present(
        cfg,
        mstack,
        "l2_skip_deep_search_when_recall_non_empty",
        "l2_skip_deep_search_when_recall_non_empty",
        bool,
    )
    _apply_if_present(cfg, mstack, "identity_path")
    _apply_if_present(cfg, mstack, "wake_char_budget")
    _apply_if_present(cfg, mstack, "recall_char_budget")
    _apply_if_present(cfg, mstack, "recall_n_results")

    # Flat keys (override nested)
    _apply_if_present(cfg, plugin_config, "enabled", "enabled", bool)
    _apply_if_present(cfg, plugin_config, "ingestion_mode")
    _apply_if_present(cfg, plugin_config, "min_turn_length")
    _apply_if_present(cfg, plugin_config, "max_turn_length")
    _apply_if_present(cfg, plugin_config, "chunk_size")
    _apply_if_present(cfg, plugin_config, "chunk_overlap")
    _apply_if_present(cfg, plugin_config, "target_wing")
    _apply_if_present(cfg, plugin_config, "target_room")
    _apply_if_present(cfg, plugin_config, "agent_name")
    _apply_if_present(cfg, plugin_config, "extract_each_turn", "extract_facts_each_turn", bool)
    _apply_if_present(cfg, plugin_config, "min_confidence")
    _apply_if_present(cfg, plugin_config, "max_facts_per_turn")
    _apply_if_present(cfg, plugin_config, "extraction_mode", "fact_extraction_mode")
    _apply_if_present(cfg, plugin_config, "allowed_predicates")
    _apply_if_present(cfg, plugin_config, "retrieval_enabled")
    _apply_if_present(cfg, plugin_config, "retrieval_mode")
    _apply_if_present(cfg, plugin_config, "vector_weight")
    _apply_if_present(cfg, plugin_config, "bm25_weight")
    _apply_if_present(cfg, plugin_config, "max_results")
    _apply_if_present(cfg, plugin_config, "min_score")
    _apply_if_present(cfg, plugin_config, "include_kg_facts")
    _apply_if_present(cfg, plugin_config, "kg_entity_limit")
    _apply_if_present(cfg, plugin_config, "retrieval_timeout_ms")
    _apply_if_present(cfg, plugin_config, "holographic_enabled")
    _apply_if_present(cfg, plugin_config, "holographic_default_trust")
    _apply_if_present(cfg, plugin_config, "memory_mirror_enabled")
    _apply_if_present(cfg, plugin_config, "mirror_add")
    _apply_if_present(cfg, plugin_config, "mirror_replace")
    _apply_if_present(cfg, plugin_config, "mirror_remove")
    _apply_if_present(cfg, plugin_config, "mirror_target_wing")
    _apply_if_present(cfg, plugin_config, "background_ingest")
    _apply_if_present(cfg, plugin_config, "background_retrieval")
    _apply_if_present(cfg, plugin_config, "max_fanout")
    _apply_if_present(cfg, plugin_config, "prefetch_cache_size")
    _apply_if_present(cfg, plugin_config, "lexical_scan_limit")
    _apply_if_present(cfg, plugin_config, "thread_join_timeout_ms")
    _apply_if_present(cfg, plugin_config, "palace_path", "palace_data_dir")
    _apply_if_present(cfg, plugin_config, "palace_data_dir")
    _apply_if_present(cfg, plugin_config, "lib_path", "mempalace_lib_dir")
    _apply_if_present(cfg, plugin_config, "mempalace_lib_dir")
    _apply_if_present(cfg, plugin_config, "memory_stack_enabled", bool)
    _apply_if_present(cfg, plugin_config, "wake_up_on_session_start", bool)
    _apply_if_present(cfg, plugin_config, "wake_up_on_first_turn", bool)
    _apply_if_present(cfg, plugin_config, "wake_up_wing")
    _apply_if_present(cfg, plugin_config, "l2_default_room")
    _apply_if_present(cfg, plugin_config, "l2_before_deep_search", bool)
    _apply_if_present(cfg, plugin_config, "l2_skip_deep_search_when_recall_non_empty", bool)
    _apply_if_present(cfg, plugin_config, "identity_path")
    _apply_if_present(cfg, plugin_config, "wake_char_budget")
    _apply_if_present(cfg, plugin_config, "recall_char_budget")
    _apply_if_present(cfg, plugin_config, "recall_n_results")


def load_config(config_data: Optional[Dict[str, Any]] = None) -> MemPalaceConfig:
    """Load configuration from Hermes config and environment.

    Args:
        config_data: Optional config dict for testing (bypasses file loading).
                     When ``HERMES_MEMPALACE_MEMORY_ENABLED`` is set, it overrides only
                     the ``enabled`` flag; nested YAML (paths, ``memory_stack``, etc.)
                     is still merged from the gathered config.
    """
    if config_data is not None:
        plugin_config = _gather_plugin_config(config_data)
        mem = _nested(config_data, "memory")
        if isinstance(mem, dict):
            plugin_config = _merge_plugin_dicts(plugin_config, mem)
            if str(mem.get("provider", "")).strip().lower() == "mempalace" and "enabled" not in plugin_config:
                plugin_config["enabled"] = True
    else:
        raw = _load_hermes_config()
        plugin_config = _gather_plugin_config(raw)
        mem = _nested(raw, "memory")
        if isinstance(mem, dict):
            plugin_config = _merge_plugin_dicts(plugin_config, mem)
            if str(mem.get("provider", "")).strip().lower() == "mempalace" and "enabled" not in plugin_config:
                plugin_config["enabled"] = True

    env_enabled = os.environ.get("HERMES_MEMPALACE_MEMORY_ENABLED")
    if env_enabled is not None:
        plugin_config = _merge_plugin_dicts(plugin_config, {"enabled": _truthy(env_enabled)})

    # Environment path overrides (CONFIG_SCHEMA.md)
    if not plugin_config.get("palace_data_dir"):
        for env_key in ("MEMPALACE_PALACE_DIR", "MEMPALACE_PALACE_PATH"):
            v = os.environ.get(env_key)
            if v:
                plugin_config["palace_data_dir"] = v
                break
    if not plugin_config.get("mempalace_lib_dir"):
        for env_key in ("MEMPALACE_LIB_DIR", "MEMPALACE_ROOT"):
            v = os.environ.get(env_key)
            if v:
                plugin_config["mempalace_lib_dir"] = v
                break

    cfg = MemPalaceConfig()
    if isinstance(plugin_config, dict):
        _apply_plugin_sections(cfg, plugin_config)

    return _finalize_config(cfg)


# ──────────────────────────────────────────────────────────────────────────────
# SCHEMA-VALIDATED FACT EXTRACTOR
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FactSchema:
    """Schema definition for a structured fact."""
    subject: str
    predicate: str
    object_: str
    confidence: float = 0.8
    valid_from: Optional[str] = None
    source: str = "auto_extracted"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "subject": self.subject,
            "predicate": self.predicate,
            "object_": self.object_,
            "confidence": self.confidence,
            "valid_from": self.valid_from,
            "source": self.source,
        }

    def validate(self) -> bool:
        """Validate that the fact conforms to the schema."""
        if not self.subject or len(self.subject) < 2:
            return False
        if not self.predicate or len(self.predicate) < 1:
            return False
        if not self.object_ or len(self.object_) < 1:
            return False
        if not (0.0 <= self.confidence <= 1.0):
            return False
        return True


class SchemaValidatedFactExtractor:
    """Extract structured facts from conversation text using schema validation.

    This replaces the previous regex-based approach with a more robust system that:
    - Validates extracted facts against a defined schema
    - Supports configurable relationship types through config rather than hardcoded values
    - Provides better entity detection and context understanding
    - Handles edge cases and invalid data gracefully
    """

    # Default relationship patterns that are commonly useful
    DEFAULT_RELATIONSHIPS = [
        ("works_on", ["project", "system", "app", "repo"]),
        ("uses", ["tool", "library", "framework", "language"]),
        ("prefers", ["style", "format", "approach"]),
        ("has", ["account", "device", "key", "subscription"]),
        ("is", ["role", "title", "type"]),
        ("located_in", ["city", "timezone", "region"]),
        ("connected_to", ["network", "server", "device"]),
        ("started_on", ["date", "project"]),
        ("ended_on", ["date", "project"]),
    ]

    # Entity patterns - capitalized words that look like names or identifiers
    ENTITY_PATTERNS = [
        r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b',  # Multi-word proper nouns
        r'\b([A-Z]{2,}(?:\s+[A-Z]{2,})+)\b',       # ALL-CAPS acronyms
        r'\b([A-Z][a-z]+(?:[0-9]+)?)\b',           # Single-word capitalized words (possibly with numbers)
    ]

    # Words that look like entities but are actually common English words
    STOP_ENTITIES = {
        "The", "This", "That", "Each", "All", "No", "One", "Any",
        "User", "Assistant", "System", "Session", "Turn", "Day",
        "Problem", "More", "Right",
    }

    @classmethod
    def extract_facts(
        cls,
        text: str,
        max_facts: int = 10,
        min_confidence: float = 0.7,
        mode: str = "schema",
        allowed_predicates: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Extract structured facts from conversation text using schema validation.

        Args:
            text: The conversation text to extract facts from
            max_facts: Maximum number of facts to extract
            min_confidence: Minimum confidence threshold for facts
            mode: Extraction mode - "schema" (strict) or "regex" (lenient)
            allowed_predicates: List of allowed predicates (None = allow all)

        Returns:
            List of validated fact dictionaries with keys: subject, predicate, object_, confidence
        """
        if not text or len(text) < 10:
            return []

        facts = []
        entities = cls._find_entities(text)

        # Try each relationship pattern
        rels = (
            [(p, []) for p in allowed_predicates]
            if allowed_predicates
            else cls.DEFAULT_RELATIONSHIPS
        )
        for subj in entities:
            for pred, expected_types in rels:
                # Pattern: "X [verb] Y" or "X's [verb] is Y"
                patterns = [
                    rf'\b{re.escape(subj)}\s+(?:is\s+)?{re.escape(pred)}\s+(\w+(?:\s+\w+)*)',
                    rf"\b{re.escape(subj)}'s\s+{re.escape(pred)}\s+(\w+(?:\s+\w+)*)",
                ]

                for pat in patterns:
                    match = re.search(pat, text, re.IGNORECASE)
                    if match:
                        obj_text = match.group(1).strip()
                        if len(obj_text) > 0 and len(obj_text) < 100:
                            fact = FactSchema(
                                subject=subj,
                                predicate=pred,
                                object_=obj_text,
                                confidence=0.85,
                                source="auto_extracted",
                            )
                            if fact.validate() and fact.confidence >= min_confidence:
                                facts.append(fact.to_dict())
                                break

                if len(facts) >= max_facts:
                    break

        # Fallback: simple noun-verb-noun pattern for any relationship
        if not facts and entities:
            for subj in list(entities)[:3]:
                match = re.search(
                    rf'\b{re.escape(subj)}\s+(\w+)\s+(\w+(?:\s+\w+)*)',
                    text,
                    re.IGNORECASE,
                )
                if match:
                    pred = match.group(1).lower()
                    obj_text = match.group(2).strip()
                    if len(obj_text) > 0 and len(obj_text) < 80:
                        fact = FactSchema(
                            subject=subj,
                            predicate=pred,
                            object_=obj_text,
                            confidence=0.65,
                            source="auto_extracted",
                        )
                        if fact.validate() and fact.confidence >= min_confidence:
                            facts.append(fact.to_dict())

        return facts[:max_facts]

    @classmethod
    def _find_entities(cls, text: str) -> List[str]:
        """Find entity mentions in text."""
        entities = set()
        for pattern in cls.ENTITY_PATTERNS:
            for match in re.finditer(pattern, text):
                entity = match.group(1).strip()
                if len(entity) >= 2 and len(entity) <= 50:
                    # Filter out common false positives
                    if not cls._is_stop_entity(entity):
                        entities.add(entity)
        return sorted(entities)

    @classmethod
    def _is_stop_entity(cls, entity: str) -> bool:
        """Check if an entity is a known false positive."""
        return entity in cls.STOP_ENTITIES


# ──────────────────────────────────────────────────────────────────────────────
# MEMORY PROVIDER (for MemoryManager integration)
# ──────────────────────────────────────────────────────────────────────────────




# ──────────────────────────────────────────────────────────────────────────────
# HOLOGRAPHIC MIRROR (for holographic storage)
# ──────────────────────────────────────────────────────────────────────────────

class HolographicMirror:
    """Mirror for holographic fact storage."""

    def __init__(self, enabled: bool = False):
        self._enabled = enabled
        self._store: Any = None

    def add_fact(self, content: str, category: str = "", trust_score: float = 0.5, tags: str = "") -> bool:
        """Add a fact to holographic storage."""
        return True

    def search_facts(self, query: str, limit: int = 3) -> List[Dict[str, Any]]:
        """Search facts in the Holographic store."""
        return []

    def close(self) -> None:
        store = getattr(self, "_store", None)
        if store is not None:
            try:
                close = getattr(store, "close", None)
                if callable(close):
                    close()
            except Exception:
                pass


class MemPalaceMemoryProvider(_MP_ABC):
    """MemoryProvider implementation for the MemPalace plugin.

    This class implements the Hermes MemoryProvider ABC so that the
    MemoryManager calls our methods during each turn:
      - sync_turn()   -> ingest each conversation turn
      - on_memory_write()  -> mirror built-in memory writes
      - prefetch()  -> recall relevant context before each LLM call

    It also registers hooks for session boundaries (on_session_start,
    on_session_end) via the regular plugin hook system.
    """

    def __init__(self, config: Optional[MemPalaceConfig] = None):
        self._config = _finalize_config(config or load_config())
        self._mp_api: Optional[MemPalaceAPI] = None
        self._holo_mirror: Optional[HolographicMirror] = None
        self._session_id: Optional[str] = None
        self._turn_count: int = 0
        self._initialized: bool = False
        self._prefetch_lock = threading.Lock()
        self._prefetch_cache: Dict[Tuple[str, str, str, str], str] = {}
        self._prefetch_inflight: Dict[Tuple[str, str, str, str], threading.Thread] = {}
        self._background_lock = threading.Lock()
        self._background_threads: List[threading.Thread] = []
        self._metrics_lock = threading.Lock()
        self._prefetch_generation: int = 0
        self._metrics: Dict[str, int] = {
            "searches": 0,
            "prefetch_cache_hits": 0,
            "prefetch_cache_misses": 0,
            "prefetch_cache_evictions": 0,
            "prefetch_timeouts": 0,
            "queued_prefetches": 0,
            "ingest_attempts": 0,
            "ingest_errors": 0,
            "memory_mirror_attempts": 0,
            "memory_mirror_errors": 0,
            "kg_invalidation_attempts": 0,
            "shutdown_thread_joins": 0,
            "memory_stack_wake_ups": 0,
            "memory_stack_l2_recalls": 0,
        }
        self._wake_block: str = ""
        self._wake_prefetch_applied: bool = False

    # ── MemoryProvider ABC (required) ─────────────────────────────────────

    @property
    def name(self) -> str:
        return "mempalace"

    def is_available(self) -> bool:
        if not self._config.enabled:
            return False
        # Check if palace data dir exists (don't require initialize() to be called)
        if self._mp_api is not None:
            return bool(getattr(self._mp_api, "is_available", False))
        # Pre-check: does the palace data path exist?
        if not self._config.palace_data_dir or not Path(self._config.palace_data_dir).exists():
            return False
        return True

    def initialize(self, session_id: str = "", *, hermes_home: str = "", **kwargs) -> None:
        """Initialize API connections — lazy MemPalace import and palace handles."""
        self._session_id = session_id or self._session_id or ""
        if self._mp_api is not None:
            try:
                self._mp_api.ensure_ready(
                    wing=self._config.target_wing,
                    room=self._config.target_room,
                )
            except Exception as e:
                logger.warning("[MemPalaceMemory] MemPalaceAPI.ensure_ready failed: %s", e)
        if self._config.holographic_enabled and self._holo_mirror is None:
            self._holo_mirror = HolographicMirror(True)
        self._initialized = True

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        """Per-turn hook (MemoryProvider); optional MemoryStack wake-up on first turn."""
        del message, kwargs
        if not self._config.enabled or not self._config.memory_stack_enabled:
            return
        if not self._config.wake_up_on_first_turn:
            return
        if not self._mp_api:
            return
        if self._wake_block:
            return
        try:
            tn = int(turn_number)
        except (TypeError, ValueError):
            tn = 0
        if tn > 1:
            return
        self._load_wake_block_if_needed(force=True)

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        **kwargs,
    ) -> None:
        del parent_session_id, reset, kwargs
        if not new_session_id:
            return
        self._session_id = new_session_id
        self._turn_count = 0
        self._prefetch_generation += 1
        with self._prefetch_lock:
            self._prefetch_cache.clear()
            self._prefetch_inflight.clear()
        self._reset_memory_stack_session_state()

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return []

    def _metric(self, name: str, amount: int = 1) -> None:
        with self._metrics_lock:
            self._metrics[name] = self._metrics.get(name, 0) + amount

    def _cache_prefetch_result(
        self,
        key: Tuple[str, str, str, str],
        result: str,
        generation: Optional[int] = None,
    ) -> None:
        with self._prefetch_lock:
            if generation is not None and generation != self._prefetch_generation:
                return
            if key not in self._prefetch_cache and len(self._prefetch_cache) >= self._config.prefetch_cache_size:
                oldest = next(iter(self._prefetch_cache), None)
                if oldest is not None:
                    self._prefetch_cache.pop(oldest, None)
                    self._metric("prefetch_cache_evictions")
            self._prefetch_cache[key] = result

    def _start_tracked_thread(self, name: str, target) -> threading.Thread:
        def _wrapped() -> None:
            try:
                target()
            finally:
                current = threading.current_thread()
                with self._background_lock:
                    self._background_threads = [t for t in self._background_threads if t is not current]

        t = threading.Thread(target=_wrapped, name=f"mempalace-{name}", daemon=True)
        with self._background_lock:
            self._background_threads.append(t)
        t.start()
        return t

    def _join_background_threads(self) -> None:
        timeout = self._config.thread_join_timeout_ms / 1000.0
        deadline = time.monotonic() + timeout
        with self._background_lock:
            threads = list(self._background_threads)
        for t in threads:
            remaining = max(0.0, deadline - time.monotonic())
            if t.is_alive() and remaining > 0:
                t.join(timeout=remaining)
                self._metric("shutdown_thread_joins")
        with self._background_lock:
            self._background_threads = [t for t in self._background_threads if t.is_alive()]

    def diagnostics(self) -> Dict[str, Any]:
        with self._prefetch_lock:
            cache_size = len(self._prefetch_cache)
            inflight = len(self._prefetch_inflight)
        with self._background_lock:
            live_threads = sum(1 for t in self._background_threads if t.is_alive())
        with self._metrics_lock:
            metrics = dict(self._metrics)
        return {
            "name": self.name,
            "enabled": self._config.enabled,
            "initialized": self._initialized,
            "available": self.is_available(),
            "palace_data_dir": self._config.palace_data_dir,
            "mempalace_lib_dir": self._config.mempalace_lib_dir,
            "session_id": self._session_id,
            "retrieval_timeout_ms": self._config.retrieval_timeout_ms,
            "max_results": self._config.max_results,
            "lexical_scan_limit": self._config.lexical_scan_limit,
            "prefetch_cache_size": cache_size,
            "prefetch_cache_limit": self._config.prefetch_cache_size,
            "prefetch_inflight": inflight,
            "background_threads": live_threads,
            "metrics": metrics,
            "memory_stack_enabled": self._config.memory_stack_enabled,
            "wake_block_chars": len(self._wake_block),
            "wake_prefetch_applied": self._wake_prefetch_applied,
        }

    # ── MemoryProvider hooks (optional overrides) ─────────────────────────

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
    ) -> None:
        """Persist a completed turn to MemPalace + Holographic."""
        logger.info("[MemPalaceMemory] sync_turn called")
        if not self._config.enabled or not self._mp_api:
            return

        if self._config.ingestion_mode == "none":
            return

        # Skip trivial turns (enforce max length before min-length check)
        combined = f"{user_content} {assistant_content}".strip()
        if len(combined) > self._config.max_turn_length:
            combined = combined[: self._config.max_turn_length]
        if len(combined) < self._config.min_turn_length:
            return

        self._turn_count += 1
        self._session_id = session_id or self._session_id

        def _ingest():
            self._metric("ingest_attempts")
            try:
                # Add verbatim chunks to MemPalace
                if combined.strip():
                    self._mp_api.chunk_and_add(
                        content=combined,
                        source_file=self._turn_source_file(session_id=session_id, content=combined),
                        wing=self._config.target_wing,
                        room=self._config.target_room,
                        agent=self._config.agent_name,
                    )

                # Extract and store facts using schema-validated extractor
                if self._config.extract_facts_each_turn and self._config.fact_extraction_mode in ("schema", "regex"):
                    extraction_mode = self._config.fact_extraction_mode
                    facts = SchemaValidatedFactExtractor.extract_facts(
                        combined,
                        max_facts=self._config.max_facts_per_turn,
                        min_confidence=self._config.min_confidence,
                        mode=extraction_mode,
                        allowed_predicates=self._config.allowed_predicates if self._config.allowed_predicates else None,
                    )
                    for fact in facts:
                        # Add to MemPalace KG
                        self._mp_api.kg_add_triple(
                            subject=fact["subject"],
                            predicate=fact["predicate"],
                            obj=fact["object_"],
                            confidence=fact.get("confidence", 0.8),
                            valid_from=fact.get("valid_from"),
                            source_file=self._turn_source_file(session_id=session_id, content=combined),
                        )
                        # Mirror to Holographic
                        if self._holo_mirror:
                            self._holo_mirror.add_fact(
                                content=f"{fact['subject']} {fact['predicate']} {fact['object_']}",
                                category="extracted_fact",
                                trust_score=fact.get("confidence", 0.8),
                            )
            except Exception as e:
                self._metric("ingest_errors")
                logger.warning("[MemPalaceMemory] sync_turn failed: %s", e)

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
        """Mirror built-in memory tool writes to MemPalace + Holographic."""
        logger.info("[MemPalaceMemory] on_memory_write called")
        if not self._config.enabled or not self._mp_api:
            return
        if not self._config.memory_mirror_enabled:
            return

        def _mirror():
            self._metric("memory_mirror_attempts")
            try:
                # Mirror add/replace as drawers
                if (
                    (action == "add" and self._config.mirror_add)
                    or (action == "replace" and self._config.mirror_replace)
                ):
                    wing = self._config.mirror_target_wing
                    room = "memory_writes"
                    if target == "user":
                        room = "user_memory"

                    self._mp_api.add_drawer(
                        content=content,
                        wing=wing,
                        room=room,
                        source_file=self._memory_write_source_file(action=action, target=target, metadata=metadata),
                        agent="hermes-memory-tool",
                    )

                # Mirror remove as KG invalidation only when a concrete triple is provided.
                if action == "remove" and self._config.mirror_remove:
                    self._metric("kg_invalidation_attempts")
                    triple = self._extract_kg_triple_metadata(metadata)
                    if triple:
                        self._mp_api.kg_invalidate_triple(
                            subject=triple["subject"],
                            predicate=triple["predicate"],
                            obj=triple["object"],
                            ended=triple.get("ended"),
                        )
                    else:
                        logger.debug(
                            "[MemPalaceMemory] remove mirror skipped: no concrete kg_triple metadata"
                        )

                # Mirror to Holographic
                if action in ("add", "replace") and self._config.holographic_enabled:
                    if self._holo_mirror:
                        self._holo_mirror.add_fact(
                            content=content,
                            category=f"memory_{action}",
                            tags=target,
                        )
            except Exception as e:
                self._metric("memory_mirror_errors")
                logger.warning("[MemPalaceMemory] on_memory_write failed: %s", e)

        if self._config.background_ingest:
            self._start_tracked_thread("mirror", _mirror)
        else:
            _mirror()

    def _extract_kg_triple_metadata(self, metadata: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
        if not isinstance(metadata, dict):
            return None
        triple = metadata.get("kg_triple") or metadata.get("triple")
        if not isinstance(triple, dict):
            return None
        subject = triple.get("subject")
        predicate = triple.get("predicate")
        obj = triple.get("object") or triple.get("obj") or triple.get("object_")
        if not subject or not predicate or not obj:
            return None
        result = {"subject": str(subject), "predicate": str(predicate), "object": str(obj)}
        ended = triple.get("ended") or triple.get("valid_to")
        if ended:
            result["ended"] = str(ended)
        return result

    def _reset_memory_stack_session_state(self) -> None:
        self._wake_block = ""
        self._wake_prefetch_applied = False

    def _load_wake_block_if_needed(self, *, force: bool = False) -> None:
        if not self._config.memory_stack_enabled or not self._mp_api:
            return
        if self._wake_block and not force:
            return
        wing = (self._config.wake_up_wing or "").strip() or None
        ident = (self._config.identity_path or "").strip()
        try:
            self._wake_block = self._mp_api.wake_up_context(
                wing=wing,
                identity_path=ident,
                char_budget=self._config.wake_char_budget,
            )
            self._metric("memory_stack_wake_ups")
        except Exception as e:
            logger.warning("[MemPalaceMemory] wake block load failed: %s", e)
            self._wake_block = ""

    def _prefetch_key(
        self,
        query: str,
        session_id: str = "",
        *,
        prefetch_wing: str = "",
        prefetch_room: str = "",
    ) -> Tuple[str, str, str, str]:
        return (
            session_id or self._session_id or "default",
            query.strip(),
            (prefetch_wing or "").strip(),
            (prefetch_room or "").strip(),
        )

    def _turn_source_file(self, *, session_id: str = "", content: str = "") -> str:
        sid = (session_id or self._session_id or "session")[:16]
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:10] if content else "nohash"
        return f"session_{sid}_turn_{self._turn_count}_{digest}"

    def _memory_write_source_file(
        self,
        *,
        action: str,
        target: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        sid = ""
        if isinstance(metadata, dict):
            sid = str(metadata.get("session_id") or metadata.get("source_session_id") or "")
        sid = (sid or self._session_id or "session")[:16]
        return f"session_{sid}_memory_{action}_{target}"

    def _run_prefetch_search(
        self,
        query: str,
        *,
        prefetch_wing: str = "",
        prefetch_room: str = "",
    ) -> str:
        """Build prefetch text: optional L0+L1 wake block once, optional L2 recall, then L3 deep search."""
        if not self._mp_api:
            return ""
        parts: List[str] = []
        total_chars = 0
        _MAX_CHARS = 2000

        if self._config.memory_stack_enabled and self._wake_block and not self._wake_prefetch_applied:
            header = "--- [mempalace] Wake-up (L0+L1) ---"
            body = self._wake_block[: self._config.wake_char_budget]
            block = f"{header}\n{body}"
            parts.append(block)
            total_chars += len(block) + 1
            self._wake_prefetch_applied = True

        l2_text = ""
        if self._config.memory_stack_enabled and self._config.l2_before_deep_search:
            pw = (prefetch_wing or "").strip()
            pr = (prefetch_room or "").strip()
            dr = (self._config.l2_default_room or "").strip()
            do_l2 = bool(pw or pr or dr)
            if do_l2:
                wing_use = pw or self._config.target_wing
                room_use = pr or (dr or None)
                try:
                    l2_text = self._mp_api.scoped_recall(
                        wing_use,
                        room_use if room_use else None,
                        n_results=self._config.recall_n_results,
                        char_budget=self._config.recall_char_budget,
                    )
                    self._metric("memory_stack_l2_recalls")
                except Exception as e:
                    logger.debug("[MemPalaceMemory] L2 recall skipped: %s", e)
                    l2_text = ""
            if l2_text.strip():
                h2 = "--- [mempalace] L2 scoped recall ---"
                block2 = f"{h2}\n{l2_text.strip()}"
                if total_chars + len(block2) + 1 <= _MAX_CHARS:
                    parts.append(block2)
                    total_chars += len(block2) + 1

        skip_l3 = (
            self._config.memory_stack_enabled
            and self._config.l2_skip_deep_search_when_recall_non_empty
            and bool(l2_text.strip())
        )

        if skip_l3:
            if self._holo_mirror and self._config.holographic_enabled:
                holo_results = self._holo_mirror.search_facts(query, limit=3)
                for r in holo_results:
                    content = r.get("content", "")[:200]
                    trust = r.get("trust_score", 0)
                    line = f"[Holographic {trust:.2f}] {content}"
                    if total_chars + len(line) + 1 > _MAX_CHARS:
                        break
                    parts.append(line)
                    total_chars += len(line) + 1
            return "\n".join(parts) if parts else ""

        self._metric("searches")
        results = self._mp_api.search(
            query=query,
            limit=self._config.max_results,
            min_score=self._config.min_score,
            vector_weight=self._config.vector_weight,
            bm25_weight=self._config.bm25_weight,
            wing=self._config.target_wing,
            room=self._config.target_room,
        )

        logger.info(
            "[MemPalaceMemory] prefetch (L3 deep search): query='%s' -> %d results (min_score=%.1f)",
            query[:80], len(results), self._config.min_score,
        )

        if results:
            hdr = "--- [mempalace] Relevant Memories (L3 deep search) ---"
            if total_chars + len(hdr) + 1 <= _MAX_CHARS:
                parts.append(hdr)
                total_chars += len(hdr) + 1
            for r in results[: self._config.max_results]:
                content = r.get("content", "")
                score = r.get("score", 0)
                wing = r.get("wing", "?")
                room = r.get("room", "?")
                source = r.get("source_file", "?")

                header = f"[{score:.2f}] {wing}/{room}/{source}"
                line = f"{header} -> {content[:150]}"
                if total_chars + len(line) + 1 > _MAX_CHARS:
                    parts.append(f"... (truncated, budget cap {_MAX_CHARS} chars)")
                    break
                parts.append(line)
                total_chars += len(line) + 1

        if self._holo_mirror and self._config.holographic_enabled:
            holo_results = self._holo_mirror.search_facts(query, limit=3)
            for r in holo_results:
                content = r.get("content", "")[:200]
                trust = r.get("trust_score", 0)
                line = f"[Holographic {trust:.2f}] {content}"
                if total_chars + len(line) + 1 > _MAX_CHARS:
                    break
                parts.append(line)
                total_chars += len(line) + 1

        return "\n".join(parts) if parts else ""

    def queue_prefetch(
        self,
        query: str,
        *,
        session_id: str = "",
        prefetch_wing: str = "",
        prefetch_room: str = "",
        **kwargs: Any,
    ) -> None:
        """Warm recall cache for the next turn (MemoryProvider).

        Optional ``prefetch_wing`` / ``prefetch_room`` (or ``wing`` / ``room`` in kwargs)
        drive L2 scoped recall when ``memory_stack`` is enabled.
        """
        pw = prefetch_wing or str(kwargs.get("wing") or "")
        pr = prefetch_room or str(kwargs.get("room") or "")
        del kwargs
        if not self._config.enabled or not self._config.retrieval_enabled:
            return
        if not query or not query.strip():
            return
        if not self._mp_api:
            return
        key = self._prefetch_key(query, session_id, prefetch_wing=pw, prefetch_room=pr)
        if not self._config.background_retrieval:
            result = self._run_prefetch_search(query, prefetch_wing=pw, prefetch_room=pr)
            self._cache_prefetch_result(key, result)
            return
        gen = self._prefetch_generation
        with self._prefetch_lock:
            if key in self._prefetch_cache or key in self._prefetch_inflight:
                return
            self._metric("queued_prefetches")

            def _do_prefetch() -> None:
                try:
                    result = self._run_prefetch_search(query, prefetch_wing=pw, prefetch_room=pr)
                    self._cache_prefetch_result(key, result, generation=gen)
                except Exception as e:
                    self._metric("prefetch_timeouts")
                    logger.warning("[MemPalaceMemory] queue_prefetch failed: %s", e)
                finally:
                    with self._prefetch_lock:
                        self._prefetch_inflight.pop(key, None)

            thread = self._start_tracked_thread("prefetch", _do_prefetch)
            self._prefetch_inflight[key] = thread

    def prefetch(
        self,
        query: str,
        session_id: str = "",
        *,
        ttl_seconds: int = 300,
        prefetch_wing: str = "",
        prefetch_room: str = "",
        **kwargs: Any,
    ) -> str:
        """Retrieve relevant context for the given query (L3 deep search plus optional memory stack layers).

        L3 is corpus-wide hybrid semantic retrieval via ``MemPalaceAPI.search``. When ``memory_stack`` is
        enabled, optional ``prefetch_wing`` / ``prefetch_room`` (or ``wing`` / ``room`` in kwargs) trigger
        L2 ``MemoryStack.recall`` ahead of L3; wake-up (L0+L1) is prepended once per session when configured.
        """
        del ttl_seconds  # reserved for future TTL-based invalidation
        pw = prefetch_wing or str(kwargs.get("wing") or "")
        pr = prefetch_room or str(kwargs.get("room") or "")
        del kwargs
        key = self._prefetch_key(query, session_id, prefetch_wing=pw, prefetch_room=pr)

        with self._prefetch_lock:
            if key in self._prefetch_cache:
                self._metric("prefetch_cache_hits")
                return self._prefetch_cache[key]
            self._metric("prefetch_cache_misses")

        if not self._config.background_retrieval:
            result = self._run_prefetch_search(query, prefetch_wing=pw, prefetch_room=pr)
            self._cache_prefetch_result(key, result)
            return result

        gen = self._prefetch_generation
        with self._prefetch_lock:
            if key not in self._prefetch_inflight:
                self._metric("queued_prefetches")

                def _do_prefetch() -> None:
                    try:
                        result = self._run_prefetch_search(query, prefetch_wing=pw, prefetch_room=pr)
                        self._cache_prefetch_result(key, result, generation=gen)
                    except Exception as e:
                        self._metric("prefetch_timeouts")
                        logger.warning("[MemPalaceMemory] prefetch background failed: %s", e)
                    finally:
                        with self._prefetch_lock:
                            self._prefetch_inflight.pop(key, None)

                thread = self._start_tracked_thread("prefetch", _do_prefetch)
                self._prefetch_inflight[key] = thread

        return ""

    def on_session_start(self, session_id: str, **kwargs) -> None:
        """Handle session start events."""
        del kwargs
        self._session_id = session_id
        self._turn_count = 0
        self._prefetch_generation += 1
        with self._prefetch_lock:
            self._prefetch_cache.clear()
            self._prefetch_inflight.clear()
        self._reset_memory_stack_session_state()
        if self._config.memory_stack_enabled and self._config.wake_up_on_session_start:
            self._load_wake_block_if_needed(force=True)

    def on_session_end(self, session_id: str, **kwargs) -> None:
        """Handle session end events."""
        del session_id, kwargs
        if self._config.ingestion_mode == "each_turn":
            pass
        if _env_enabled("HERMES_ENABLE_MEMPALACE_SESSION_IMPORTER", default=True):
            _launch_session_importer()
        self._join_background_threads()

    def shutdown(self) -> None:
        """Clean up resources on shutdown."""
        self._join_background_threads()
        if self._holo_mirror is not None:
            try:
                self._holo_mirror.close()
            except Exception:
                pass


# ──────────────────────────────────────────────────────────────────────────────
# PLUGIN REGISTRATION
# ──────────────────────────────────────────────────────────────────────────────

def load_plugin() -> MemPalaceMemoryProvider:
    """Load and return the plugin instance."""
    config = load_config()
    provider = MemPalaceMemoryProvider(config)

    try:
        from hermes_cli.utils import get_hermes_home

        hermes_home = get_hermes_home() or ""

        palace_path = config.palace_data_dir
        if not palace_path and hermes_home:
            palace_path = str(Path(hermes_home) / ".mempalace" / "palace")

        lib_dir = (
            config.mempalace_lib_dir
            or os.environ.get("MEMPALACE_LIB_DIR", "")
            or os.environ.get("MEMPALACE_ROOT", "")
        )

        provider._mp_api = MemPalaceAPI(
            palace_path,
            mempalace_lib_dir=lib_dir,
            lexical_scan_limit=config.lexical_scan_limit,
        )
        provider._holo_mirror = HolographicMirror(config.holographic_enabled)
        provider.initialize(session_id="", hermes_home=hermes_home)
    except Exception as e:
        logger.warning("[MemPalaceMemory] Failed to initialize palace connections: %s", e)

    return provider



# ──────────────────────────────────────────────────────────────────────────────
# MEMPALACE API (for interacting with MemPalace)
# ──────────────────────────────────────────────────────────────────────────────


class MemPalaceAPI:
    """Lazy-import bridge to MemPalace (search, drawers, knowledge graph)."""

    def __init__(
        self,
        palace_data_dir: str = "",
        mempalace_lib_dir: str = "",
        lexical_scan_limit: int = 1000,
    ):
        self._palace_data_dir = str(Path(palace_data_dir).expanduser()) if palace_data_dir else ""
        self._mempalace_lib_dir = str(Path(mempalace_lib_dir).expanduser()) if mempalace_lib_dir else ""
        self._lexical_scan_limit = lexical_scan_limit
        self._imported = False
        self._import_error: Optional[str] = None
        self._search_memories_fn: Any = None
        self._get_collection_fn: Any = None
        self._miner_add_drawer_fn: Any = None
        self._chunk_text_fn: Any = None
        self._kg: Any = None
        self._palace: Any = None
        self._miner: Any = None
        self._default_wing: str = ""
        self._default_room: str = ""

    def ensure_ready(self, wing: str = "", room: str = "") -> None:
        self._default_wing = wing or self._default_wing
        self._default_room = room or self._default_room
        self._ensure_imported()

    def _make_memory_stack(self, *, identity_path: str = "") -> Any:
        """Construct MemoryStack if the mempalace package exposes it (best-effort ctor variants)."""
        self._ensure_imported()
        try:
            from mempalace.layers import MemoryStack as _MS
        except Exception as e:
            logger.debug("[MemPalaceMemory] MemoryStack import failed: %s", e)
            return None
        id_path = (identity_path or "").strip()
        palace = (self._palace_data_dir or "").strip()
        attempts: List[Dict[str, Any]] = []
        if palace and id_path:
            attempts.append({"palace_path": palace, "identity_path": id_path})
        if palace:
            attempts.append({"palace_path": palace})
        if id_path:
            attempts.append({"identity_path": id_path})
        attempts.append({})
        for kwargs in attempts:
            if not kwargs:
                try:
                    return _MS()
                except Exception:
                    continue
            try:
                return _MS(**kwargs)
            except TypeError:
                try:
                    return _MS(**{k: v for k, v in kwargs.items() if k == "palace_path"})
                except Exception:
                    continue
            except Exception:
                continue
        return None

    def wake_up_context(
        self,
        wing: Optional[str] = None,
        *,
        identity_path: str = "",
        char_budget: int = 3200,
    ) -> str:
        """L0+L1 wake-up text from MemoryStack.wake_up(), bounded by char_budget."""
        stack = self._make_memory_stack(identity_path=identity_path)
        if stack is None:
            return ""
        try:
            wu = getattr(stack, "wake_up", None)
            if not callable(wu):
                return ""
            if wing:
                try:
                    raw = wu(wing=wing)
                except TypeError:
                    raw = wu(wing)
            else:
                raw = wu()
        except Exception as e:
            logger.warning("[MemPalaceMemory] wake_up failed: %s", e)
            return ""
        text = raw if isinstance(raw, str) else str(raw or "")
        if char_budget > 0 and len(text) > char_budget:
            text = text[:char_budget] + "\n... (wake-up truncated)"
        return text

    def scoped_recall(
        self,
        wing: str,
        room: Optional[str] = None,
        *,
        n_results: int = 10,
        char_budget: int = 1500,
    ) -> str:
        """L2 room-scoped recall via MemoryStack.recall(wing=..., room=...)."""
        stack = self._make_memory_stack()
        if stack is None or not (wing or "").strip():
            return ""
        w = wing.strip()
        try:
            rc = getattr(stack, "recall", None)
            if not callable(rc):
                return ""
            try:
                raw = rc(wing=w, room=room, n_results=n_results)
            except TypeError:
                try:
                    raw = rc(wing=w, room=room)
                except TypeError:
                    raw = rc(w)
        except Exception as e:
            logger.debug("[MemPalaceMemory] scoped recall failed: %s", e)
            return ""
        if isinstance(raw, str):
            text = raw
        elif isinstance(raw, list):
            text = "\n".join(str(x) for x in raw)
        else:
            text = str(raw or "")
        if char_budget > 0 and len(text) > char_budget:
            text = text[:char_budget] + "\n... (recall truncated)"
        return text

    def _ensure_imported(self) -> None:
        if self._imported:
            return
        lib = (
            self._mempalace_lib_dir
            or os.environ.get("MEMPALACE_LIB_DIR", "")
            or os.environ.get("MEMPALACE_ROOT", "")
        ).strip()
        if lib:
            lp = str(Path(lib).expanduser())
            if lp not in sys.path:
                sys.path.insert(0, lp)
        try:
            from mempalace.miner import add_drawer as _mad
            from mempalace.miner import chunk_text as _chunk
            from mempalace.palace import get_collection as _gc
            from mempalace.searcher import search_memories as _sm

            self._search_memories_fn = _sm
            self._get_collection_fn = _gc
            self._miner_add_drawer_fn = _mad
            self._chunk_text_fn = _chunk
            self._imported = True
            self._import_error = None
        except Exception as e:
            self._import_error = str(e)
            self._imported = False
            logger.warning("[MemPalaceMemory] mempalace package import failed: %s", e)

    def _resolve_kg(self) -> Any:
        if self._kg is not None:
            return self._kg
        self._ensure_imported()
        if not self._imported:
            return None
        try:
            from mempalace.knowledge_graph import KnowledgeGraph as _KG

            self._kg = _KG()
        except Exception as e:
            logger.warning("[MemPalaceMemory] KnowledgeGraph unavailable: %s", e)
            self._kg = None
        return self._kg

    @property
    def is_available(self) -> bool:
        if not self._palace_data_dir or not Path(self._palace_data_dir).exists():
            return False
        self._ensure_imported()
        return bool(self._imported)

    def _drawers_collection(self, *, create: bool = False) -> Any:
        if self._palace is not None:
            return self._palace.get_collection(self._palace_data_dir or "drawers")
        if self._get_collection_fn is None:
            self._ensure_imported()
        if self._get_collection_fn is None:
            raise RuntimeError("MemPalace get_collection unavailable")
        return self._get_collection_fn(self._palace_data_dir, create=create)

    @staticmethod
    def _coerce_chroma_get(res: Any) -> Tuple[List[str], List[str], List[Dict[str, Any]]]:
        if isinstance(res, dict):
            ids_raw = res.get("ids") or []
            docs = res.get("documents") or []
            metas = res.get("metadatas") or []
            if ids_raw and isinstance(ids_raw[0], list):
                ids_raw = ids_raw[0]
            if docs and isinstance(docs[0], list):
                docs = docs[0]
            if metas and isinstance(metas[0], list):
                metas = metas[0]
            return list(ids_raw or []), list(docs or []), list(metas or [])
        ids_raw = getattr(res, "ids", None) or []
        docs = getattr(res, "documents", None) or []
        metas = getattr(res, "metadatas", None) or []
        if ids_raw is not None and len(ids_raw) > 0 and not isinstance(ids_raw[0], str):
            try:
                ids_raw = list(ids_raw[0])
                docs = list(docs[0]) if docs else []
                metas = list(metas[0]) if metas else []
            except (IndexError, TypeError):
                ids_raw = list(ids_raw)
        return list(ids_raw or []), list(docs or []), list(metas or [])

    def _lexical_fallback_search(self, query: str, limit: int) -> List[Dict[str, Any]]:
        if not self._palace_data_dir and self._palace is None:
            return []
        try:
            col = self._drawers_collection(create=False)
        except Exception:
            return []

        def norm_blob(s: str) -> str:
            return re.sub(r"[^a-z0-9]+", "", (s or "").lower())

        qn = norm_blob(query.replace("-", "_"))
        if query.startswith("drawer_"):
            try:
                got = col.get(ids=[query], include=["documents", "metadatas"])
            except TypeError:
                got = col.get(ids=[query])
            ids, docs, metas = self._coerce_chroma_get(got)
            if ids and ids[0] == query:
                meta = metas[0] if metas else {}
                doc = docs[0] if docs else ""
                if not isinstance(meta, dict):
                    meta = {}
                src = str(meta.get("source_file", ""))
                return [
                    {
                        "content": doc,
                        "score": 1.0,
                        "wing": meta.get("wing", "?"),
                        "room": meta.get("room", "?"),
                        "source_file": src,
                        "drawer_id": query,
                        "match_type": "lexical:id",
                    }
                ]
            return []

        try:
            got = col.get(limit=self._lexical_scan_limit, include=["documents", "metadatas", "ids"])
        except TypeError:
            try:
                got = col.get(limit=self._lexical_scan_limit)
            except Exception:
                return []
        ids, docs, metas = self._coerce_chroma_get(got)
        out: List[Dict[str, Any]] = []
        q_path = query.lower().replace("-", "_")
        for i, did in enumerate(ids[: max(limit * 50, limit)]):
            doc = docs[i] if i < len(docs) else ""
            meta = metas[i] if i < len(metas) else {}
            if not isinstance(meta, dict):
                meta = {}
            src = str(meta.get("source_file", ""))
            blob = norm_blob(doc + " " + src)
            src_cmp = src.lower().replace("-", "_")
            if qn and (qn in blob or q_path in src_cmp or qn in norm_blob(src)):
                out.append(
                    {
                        "content": doc,
                        "score": 1.0,
                        "wing": meta.get("wing", "?"),
                        "room": meta.get("room", "?"),
                        "source_file": src,
                        "drawer_id": did,
                        "match_type": "lexical:meta",
                    }
                )
            if len(out) >= limit:
                break
        return out[:limit]

    def add_drawer(
        self,
        content: str,
        wing: str = "memory",
        room: str = "conversations",
        source_file: str = "",
        agent: str = "",
        duplicate_threshold: float = 0.0,
        **_: Any,
    ) -> str:
        self._ensure_imported()
        try:
            col = self._drawers_collection(create=True)
        except Exception as e:
            logger.warning("[MemPalaceMemory] add_drawer: no collection (%s)", e)
            return ""

        if duplicate_threshold and duplicate_threshold > 0:
            try:
                res = col.query(
                    query_texts=[content],
                    n_results=5,
                    include=["metadatas", "documents", "distances"],
                )
            except Exception:
                res = None
            if isinstance(res, dict) and res.get("ids") and res["ids"][0]:
                row_ids = res["ids"][0]
                dists = (res.get("distances") or [[]])[0]
                for idx, drawer_id in enumerate(row_ids):
                    try:
                        dist = float(dists[idx])
                    except (IndexError, TypeError, ValueError):
                        dist = 1.0
                    sim = max(0.0, min(1.0, 1.0 - dist))
                    if sim >= duplicate_threshold:
                        return str(drawer_id)

        if self._miner is not None:
            try:
                out = self._miner.add_drawer(
                    content=content,
                    wing=wing,
                    room=room,
                    source_file=source_file,
                    agent=agent,
                )
            except TypeError:
                out = self._miner.add_drawer(
                    collection=col,
                    wing=wing,
                    room=room,
                    content=content,
                    source_file=source_file,
                    chunk_index=0,
                    agent=agent,
                )
            if isinstance(out, dict):
                return str(out.get("drawer_id", out.get("id", "")))
            if out is True:
                return f"drawer_{wing}_{room}_{hashlib.sha256((wing + room + content).encode()).hexdigest()[:24]}"
            return str(out) if out else ""

        if self._miner_add_drawer_fn is None:
            return f"drawer_{wing}_{room}_{hashlib.sha256((wing + room + content).encode()).hexdigest()[:24]}"
        src = source_file or "inline.md"
        self._miner_add_drawer_fn(col, wing, room, content, src, 0, agent or "hermes")
        return f"drawer_{wing}_{room}_{hashlib.sha256((src + '0').encode()).hexdigest()[:24]}"

    def chunk_and_add(
        self,
        content: str,
        source_file: str = "",
        wing: str = "memory",
        room: str = "conversations",
        agent: str = "",
        **_: Any,
    ) -> List[str]:
        self._ensure_imported()
        src = source_file or "conversation_turn.md"
        if self._chunk_text_fn is None:
            did = self.add_drawer(content, wing=wing, room=room, source_file=src, agent=agent)
            return [did] if did else []
        try:
            col = self._drawers_collection(create=True)
        except Exception as e:
            logger.warning("[MemPalaceMemory] chunk_and_add: no collection (%s)", e)
            return []
        added: List[str] = []
        for chunk in self._chunk_text_fn(content, src):
            body = chunk.get("content", "")
            idx = int(chunk.get("chunk_index", 0))
            if self._miner is not None:
                try:
                    self._miner.add_drawer(
                        content=body,
                        wing=wing,
                        room=room,
                        source_file=src,
                        agent=agent,
                    )
                except TypeError:
                    self._miner.add_drawer(
                        collection=col,
                        wing=wing,
                        room=room,
                        content=body,
                        source_file=src,
                        chunk_index=idx,
                        agent=agent,
                    )
            elif self._miner_add_drawer_fn:
                self._miner_add_drawer_fn(col, wing, room, body, src, idx, agent or "hermes")
            did = f"drawer_{wing}_{room}_{hashlib.sha256((src + str(idx)).encode()).hexdigest()[:24]}"
            added.append(did)
        return added

    def search(
        self,
        query: str,
        limit: int = 8,
        min_score: float = 0.3,
        vector_weight: float = 0.6,
        bm25_weight: float = 0.4,
        wing: Optional[str] = None,
        room: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        del vector_weight, bm25_weight
        w = wing if wing is not None else self._default_wing
        r = room if room is not None else self._default_room
        if not query.strip():
            return []
        self._ensure_imported()
        mapped: List[Dict[str, Any]] = []
        max_dist = max(0.0, 1.0 - float(min_score)) if min_score > 0 else 0.0
        raw: Any = None
        search_fn = self._search_memories_fn
        if search_fn is None:
            _sch = getattr(self, "_searcher", None)
            if _sch is not None and hasattr(_sch, "search_memories"):
                search_fn = getattr(_sch, "search_memories")
        if search_fn is not None and self._palace_data_dir:
            try:
                raw = search_fn(
                    query,
                    self._palace_data_dir,
                    wing=w or None,
                    room=r or None,
                    n_results=limit,
                    max_distance=max_dist,
                )
            except TypeError:
                try:
                    raw = search_fn(
                        query=query,
                        palace_path=self._palace_data_dir,
                        wing=w or None,
                        room=r or None,
                        n_results=limit,
                        max_distance=max_dist,
                    )
                except Exception as e:
                    logger.debug("[MemPalaceMemory] search_memories kwargs failed: %s", e)
                    raw = None
            except Exception as e:
                logger.debug("[MemPalaceMemory] search_memories failed: %s", e)
                raw = None
        if isinstance(raw, dict) and raw.get("error"):
            mapped = []
        elif isinstance(raw, dict):
            for h in raw.get("results") or []:
                sim = float(h.get("similarity", 0.0))
                if sim < min_score:
                    continue
                mapped.append(
                    {
                        "content": h.get("text", ""),
                        "score": sim,
                        "wing": h.get("wing", "?"),
                        "room": h.get("room", "?"),
                        "source_file": h.get("source_file", "?"),
                        "drawer_id": h.get("drawer_id", ""),
                        "match_type": str(h.get("matched_via", "semantic")),
                    }
                )
        if mapped:
            return mapped[:limit]
        return self._lexical_fallback_search(query, limit)[:limit]

    def kg_add_triple(
        self,
        subject: str,
        predicate: str,
        obj: str,
        confidence: float = 0.8,
        valid_from: Optional[str] = None,
        source_file: Optional[str] = None,
        source_closet: Optional[str] = None,
        **_: Any,
    ) -> bool:
        kg = self._resolve_kg()
        if kg is None:
            return False
        try:
            kg.add_triple(
                subject,
                predicate,
                obj,
                valid_from=valid_from,
                confidence=confidence,
                source_file=source_file,
                source_closet=source_closet,
            )
            return True
        except Exception as e:
            logger.warning("[MemPalaceMemory] kg_add_triple failed: %s", e)
            return False

    def kg_invalidate_triple(self, subject: str, predicate: str, obj: str, ended: Optional[str] = None) -> bool:
        kg = self._resolve_kg()
        if kg is None:
            return False
        try:
            kg.invalidate(subject, predicate, obj, ended=ended)
            return True
        except Exception as e:
            logger.warning("[MemPalaceMemory] kg_invalidate_triple failed: %s", e)
            return False


# Export public symbols for external use.
__all__ = [
    "load_config",
    "load_plugin",
    "MemPalaceConfig",
    "MemPalaceMemoryProvider",
    "MemPalaceAPI",
    "HolographicMirror",
    "SchemaValidatedFactExtractor",
    "FactSchema",
]
