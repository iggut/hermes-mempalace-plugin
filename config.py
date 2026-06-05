"""MemPalace plugin configuration — dataclass, load, clamp."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _falsey(value: Any) -> bool:
    return str(value).strip().lower() in ("0", "false", "no", "off")


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


def _nested(config: Dict[str, Any], *keys: str) -> Any:
    cur: Any = config
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def _apply_if_present(cfg, data: Dict[str, Any], key: str, attr: str = "", cast=None):
    if not isinstance(data, dict) or key not in data:
        return
    value = data[key]
    if cast is bool:
        if _truthy(value):
            value = True
        elif _falsey(value):
            value = False
        elif isinstance(value, bool):
            pass
        else:
            logger.warning("[MemPalaceConfig] Invalid boolean %s=%r", key, value)
            return
    elif cast is not None:
        try:
            value = cast(value)
        except (TypeError, ValueError):
            logger.warning("[MemPalaceConfig] Invalid config %s=%r", key, value)
            return
    setattr(cfg, attr or key, value)


@dataclass
class MemPalaceConfig:
    """MemPalace memory provider configuration.

    All feature flags default to off. Retrieval is on by default.
    """

    # Core
    enabled: bool = True
    palace_data_dir: str = ""
    mempalace_lib_dir: str = ""

    # Duplicate check (applies to all write paths)
    duplicate_check_enabled: bool = True
    duplicate_threshold: float = 0.9  # cosine distance

    # Ingestion (off by default)
    ingestion_mode: str = "none"  # each_turn | session_end | none
    min_turn_length: int = 20
    max_turn_length: int = 8000
    chunk_size: int = 800
    chunk_overlap: int = 100
    target_wing: str = "memory"
    target_room: str = "conversations"
    agent_name: str = "jupiter"
    # Strip system tags, hook output, and UI chrome before ingestion.
    # Uses mempalace.normalize.strip_noise — line-anchored, verbatim-safe.
    normalize_content: bool = True

    # Fact extraction (off by default)
    extract_facts_each_turn: bool = False
    fact_extraction_mode: str = "schema"
    min_confidence: float = 0.7
    max_facts_per_turn: int = 10
    allowed_predicates: List[str] = field(default_factory=list)

    # Entity detector mode (off by default)
    fact_extraction_entity_detector: bool = False

    # Retrieval (on by default)
    retrieval_enabled: bool = True
    retrieval_mode: str = "hybrid"
    vector_weight: float = 0.6
    bm25_weight: float = 0.4
    max_results: int = 8
    min_score: float = 0.5
    # Recency boost: drawers with created_at within this many days are
    # prioritized over older hits with the same score (set to 0 to disable).
    prioritize_recent_days: int = 30
    include_kg_facts: bool = False  # off by default
    kg_entity_limit: int = 5
    retrieval_timeout_ms: int = 500
    # cache_ttl_seconds: how long a cached prefetch result stays valid
    cache_ttl_seconds: int = 30

    # Graph (off by default)
    graph_enabled: bool = False
    graph_traverse_max_hops: int = 2
    graph_traverse_limit: int = 10
    graph_find_tunnels: bool = False

    # Holographic (off by default)
    holographic_enabled: bool = False
    holographic_default_trust: float = 0.5

    # Memory mirror (off by default)
    memory_mirror_enabled: bool = False
    mirror_add: bool = True
    mirror_replace: bool = True
    mirror_remove: bool = True
    mirror_target_wing: str = "memory"

    # Diary (off by default)
    diary_enabled: bool = False
    diary_agent_name: str = ""
    diary_wing: str = ""
    diary_topic: str = "session_summary"
    diary_read_on_start: bool = False
    diary_last_n: int = 5

    # AAAK dialect (off by default — lossy)
    aaak_enabled: bool = False
    aaak_compress_digests: bool = False
    aaak_config_path: str = ""

    # Performance
    background_ingest: bool = True
    background_retrieval: bool = True
    max_fanout: int = 10
    prefetch_cache_size: int = 32
    lexical_scan_limit: int = 1000
    thread_join_timeout_ms: int = 1000

    # Memory stack (default ON: real MemoryStack L0+L1 wake adds ~1 hit per
    # query and ~378 chars of top-importance structured context. Latency
    # cost ~10-20ms. See 2026-06-02 feature audit in CHANGELOG.md.)
    memory_stack_enabled: bool = True
    # Wake on session start (default ON): loads L0 identity + L1 essentials
    # at the start of each session. Skipping it forces a first-turn wake
    # which adds latency to the first user-visible response.
    wake_up_on_session_start: bool = True
    wake_up_on_first_turn: bool = False
    wake_up_wing: str = ""
    l2_default_room: str = ""
    l2_before_deep_search: bool = True
    l2_skip_deep_search_when_recall_non_empty: bool = False
    identity_path: str = ""
    wake_char_budget: int = 3200
    recall_char_budget: int = 1500
    recall_n_results: int = 10

    # Staged recall (Phase 3 — L0/L1/L2/L3 pipeline)
    # L0: tiny wake context from memory stack
    max_wake_block_chars: int = 600
    # L2: targeted scoped recall
    prefer_active_project: bool = True
    use_kg: bool = False
    use_halls: bool = False
    use_closets: bool = False
    # L3: full search fallback (only runs when L2 finds nothing strong/medium)
    # Default True: live audit (2026-06-02) showed L2 misses short-token queries
    # like "mempalace" and "Hermes" by 6 hits vs L3 always-on. Latency stays
    # under 200ms with both on; no timeouts. Set False only if you want
    # minimum-cost corpus-wide suppression.
    always_run_l3: bool = True
    max_l3_search_time_ms: int = 400
    # Token budget for recall injection
    max_recall_chars: int = 3500
    max_quote_chars_per_hit: int = 320
    max_total_quoted_chars: int = 2400
    # Cross-wing tunnels
    follow_tunnels: bool = False
    max_tunnel_hops: int = 1
    max_tunnel_hits: int = 2
    # Dynamics (Hebbian + Ebbinghaus) — default ON; uses MemPalace's
    # mempalace.dynamics module to strength-sort hits and reinforce the
    # connections the user actually sees. Requires halls + tunnels
    # persistence to be available (i.e. the palace has graph data). Set
    # false to fall back to the hand-rolled recency boost only.
    dynamics_enabled: bool = True
    # Duplicate guard for session importer
    avoid_duplicate_session_imports: bool = True

    @property
    def retrieval_timeout_seconds(self) -> float:
        """Bounded retrieval timeout in seconds (derived from retrieval_timeout_ms)."""
        return max(0.05, self.retrieval_timeout_ms / 1000.0)


def _finalize_config(cfg: MemPalaceConfig) -> MemPalaceConfig:
    """Clamp all numeric fields to safe production bounds."""
    cfg.ingestion_mode = cfg.ingestion_mode if cfg.ingestion_mode in (
        "each_turn", "session_end", "none"
    ) else "none"
    cfg.retrieval_mode = cfg.retrieval_mode if cfg.retrieval_mode in (
        "vector", "bm25", "hybrid"
    ) else "hybrid"

    if cfg.palace_data_dir:
        cfg.palace_data_dir = str(Path(cfg.palace_data_dir).expanduser())
    if cfg.mempalace_lib_dir:
        cfg.mempalace_lib_dir = str(Path(cfg.mempalace_lib_dir).expanduser())

    # Clamp numerics
    cfg.min_turn_length = _clamp(cfg.min_turn_length, 10, 5000, 20)
    cfg.max_turn_length = _clamp(cfg.max_turn_length, 12, 50000, 8000)
    cfg.chunk_size = _clamp(cfg.chunk_size, 100, 10000, 800)
    cfg.chunk_overlap = _clamp(cfg.chunk_overlap, 0, cfg.chunk_size // 2, 100)
    cfg.max_facts_per_turn = _clamp(cfg.max_facts_per_turn, 1, 50, 10)
    cfg.max_results = _clamp(cfg.max_results, 1, 50, 8)
    cfg.kg_entity_limit = _clamp(cfg.kg_entity_limit, 1, 20, 5)
    cfg.retrieval_timeout_ms = _clamp(cfg.retrieval_timeout_ms, 50, 5000, 500)
    cfg.max_fanout = _clamp(cfg.max_fanout, 1, 50, 10)
    cfg.prefetch_cache_size = _clamp(cfg.prefetch_cache_size, 1, 200, 32)
    cfg.lexical_scan_limit = _clamp(cfg.lexical_scan_limit, 10, 5000, 1000)
    cfg.thread_join_timeout_ms = _clamp(cfg.thread_join_timeout_ms, 100, 10000, 1000)
    cfg.wake_char_budget = _clamp(cfg.wake_char_budget, 200, 20000, 3200)
    cfg.recall_char_budget = _clamp(cfg.recall_char_budget, 200, 10000, 1500)
    cfg.recall_n_results = _clamp(cfg.recall_n_results, 1, 50, 10)
    cfg.graph_traverse_max_hops = _clamp(cfg.graph_traverse_max_hops, 1, 5, 2)
    cfg.graph_traverse_limit = _clamp(cfg.graph_traverse_limit, 1, 50, 10)
    cfg.diary_last_n = _clamp(cfg.diary_last_n, 1, 100, 5)
    cfg.cache_ttl_seconds = _clamp(cfg.cache_ttl_seconds, 1, 300, 30)

    cfg.max_wake_block_chars = _clamp(cfg.max_wake_block_chars, 100, 5000, 600)
    cfg.max_recall_chars = _clamp(cfg.max_recall_chars, 200, 8000, 3500)
    cfg.max_quote_chars_per_hit = _clamp(cfg.max_quote_chars_per_hit, 50, 2000, 320)
    cfg.max_total_quoted_chars = _clamp(cfg.max_total_quoted_chars, 100, 5000, 2400)
    cfg.max_l3_search_time_ms = _clamp(cfg.max_l3_search_time_ms, 50, 2000, 400)
    cfg.max_tunnel_hops = _clamp(cfg.max_tunnel_hops, 1, 5, 1)
    cfg.max_tunnel_hits = _clamp(cfg.max_tunnel_hits, 1, 10, 2)

    cfg.min_score = _clamp_float(cfg.min_score, 0.0, 1.0, 0.5)
    cfg.prioritize_recent_days = _clamp(cfg.prioritize_recent_days, 0, 365, 30)
    cfg.vector_weight = _clamp_float(cfg.vector_weight, 0.0, 1.0, 0.6)
    cfg.bm25_weight = _clamp_float(cfg.bm25_weight, 0.0, 1.0, 0.4)
    cfg.holographic_default_trust = _clamp_float(
        cfg.holographic_default_trust, 0.0, 1.0, 0.5
    )
    cfg.duplicate_threshold = _clamp_float(cfg.duplicate_threshold, 0.0, 1.0, 0.9)

    if cfg.fact_extraction_mode not in {"none", "regex", "schema", "entity_detector"}:
        cfg.fact_extraction_mode = "schema"

    if not cfg.palace_data_dir and os.environ.get("HOME"):
        cfg.palace_data_dir = str(Path(os.environ["HOME"]) / ".mempalace" / "palace")

    return cfg


def _merge_plugin_dicts(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Shallow-merge, with nested dict merge for known sections."""
    out = dict(base)
    for key, val in overlay.items():
        if (
            key
            in (
                "ingestion", "facts", "retrieval", "performance",
                "holographic", "memory_mirror", "memory_stack",
                "graph", "diary", "aaak",
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
    """Merge all config branches that may carry MemPalace settings."""
    merged: Dict[str, Any] = {}
    for branch in [
        _nested(raw, "plugins", "mempalace"),
        _nested(raw, "plugins", "mempalace_memory"),
        _nested(raw, "mempalace_memory"),
    ]:
        if isinstance(branch, dict):
            merged = _merge_plugin_dicts(merged, branch)
    return merged


def _apply_plugin_sections(cfg: MemPalaceConfig, plugin_config: Dict[str, Any]) -> None:
    """Apply nested and flat config keys to cfg."""
    def g(name):
        return plugin_config.get(name) if isinstance(plugin_config.get(name), dict) else {}

    ing = g("ingestion")
    _apply_if_present(cfg, ing, "mode", "ingestion_mode")
    _apply_if_present(cfg, ing, "min_turn_length")
    _apply_if_present(cfg, ing, "max_turn_length")
    _apply_if_present(cfg, ing, "chunk_size")
    _apply_if_present(cfg, ing, "chunk_overlap")
    _apply_if_present(cfg, ing, "wing", "target_wing")
    _apply_if_present(cfg, ing, "room", "target_room")
    _apply_if_present(cfg, ing, "agent", "agent_name")
    _apply_if_present(cfg, ing, "normalize_content", "normalize_content", bool)

    facts = g("facts")
    _apply_if_present(cfg, facts, "extract_each_turn", "extract_facts_each_turn", bool)
    _apply_if_present(cfg, facts, "min_confidence")
    _apply_if_present(cfg, facts, "max_facts_per_turn")
    _apply_if_present(cfg, facts, "extraction_mode", "fact_extraction_mode")
    _apply_if_present(cfg, facts, "allowed_predicates")
    _apply_if_present(cfg, facts, "use_entity_detector", "fact_extraction_entity_detector", bool)

    retr = g("retrieval")
    _apply_if_present(cfg, retr, "enabled", "retrieval_enabled", bool)
    _apply_if_present(cfg, retr, "mode", "retrieval_mode")
    _apply_if_present(cfg, retr, "vector_weight")
    _apply_if_present(cfg, retr, "bm25_weight")
    _apply_if_present(cfg, retr, "max_results")
    _apply_if_present(cfg, retr, "min_score")
    _apply_if_present(cfg, retr, "prioritize_recent_days")
    _apply_if_present(cfg, retr, "include_kg_facts", None, bool)
    _apply_if_present(cfg, retr, "kg_entity_limit")
    _apply_if_present(cfg, retr, "timeout_ms", "retrieval_timeout_ms")
    _apply_if_present(cfg, retr, "max_recall_chars")
    _apply_if_present(cfg, retr, "max_quote_chars_per_hit")
    _apply_if_present(cfg, retr, "max_total_quoted_chars")
    _apply_if_present(cfg, retr, "max_l3_search_time_ms")
    _apply_if_present(cfg, retr, "prefer_active_project", None, bool)
    _apply_if_present(cfg, retr, "use_kg", None, bool)
    _apply_if_present(cfg, retr, "use_halls", None, bool)
    _apply_if_present(cfg, retr, "use_closets", None, bool)
    _apply_if_present(cfg, retr, "follow_tunnels", None, bool)
    _apply_if_present(cfg, retr, "max_tunnel_hops")
    _apply_if_present(cfg, retr, "max_tunnel_hits")
    _apply_if_present(cfg, retr, "always_run_l3", None, bool)
    _apply_if_present(cfg, retr, "dynamics_enabled", None, bool)

    perf = g("performance")
    _apply_if_present(cfg, perf, "background_ingest")
    _apply_if_present(cfg, perf, "background_retrieval")
    _apply_if_present(cfg, perf, "max_fanout")
    _apply_if_present(cfg, perf, "prefetch_cache_size")
    _apply_if_present(cfg, perf, "lexical_scan_limit")
    _apply_if_present(cfg, perf, "thread_join_timeout_ms")

    holo = g("holographic")
    _apply_if_present(cfg, holo, "enabled", "holographic_enabled", bool)
    _apply_if_present(cfg, holo, "default_trust", "holographic_default_trust")

    mir = g("memory_mirror")
    _apply_if_present(cfg, mir, "enabled", "memory_mirror_enabled", bool)
    _apply_if_present(cfg, mir, "mirror_add")
    _apply_if_present(cfg, mir, "mirror_replace")
    _apply_if_present(cfg, mir, "mirror_remove")
    _apply_if_present(cfg, mir, "target_wing", "mirror_target_wing")

    diary = g("diary")
    _apply_if_present(cfg, diary, "enabled", "diary_enabled", bool)
    _apply_if_present(cfg, diary, "agent_name", "diary_agent_name")
    _apply_if_present(cfg, diary, "wing", "diary_wing")
    _apply_if_present(cfg, diary, "topic", "diary_topic")
    _apply_if_present(cfg, diary, "read_on_start", "diary_read_on_start", bool)
    _apply_if_present(cfg, diary, "last_n", "diary_last_n")

    aaak = g("aaak")
    _apply_if_present(cfg, aaak, "enabled", "aaak_enabled", bool)
    _apply_if_present(cfg, aaak, "compress_digests", "aaak_compress_digests", bool)
    _apply_if_present(cfg, aaak, "config_path", "aaak_config_path")

    mstack = g("memory_stack")
    _apply_if_present(cfg, mstack, "enabled", "memory_stack_enabled", bool)
    _apply_if_present(cfg, mstack, "wake_up_on_session_start", "wake_up_on_session_start", bool)
    _apply_if_present(cfg, mstack, "wake_up_on_first_turn", "wake_up_on_first_turn", bool)
    _apply_if_present(cfg, mstack, "wake_up_wing")
    _apply_if_present(cfg, mstack, "l2_room", "l2_default_room")
    _apply_if_present(cfg, mstack, "l2_default_room")
    _apply_if_present(cfg, mstack, "l2_before_deep_search", "l2_before_deep_search", bool)
    _apply_if_present(cfg, mstack, "l2_skip_deep_search_when_recall_non_empty",
                     "l2_skip_deep_search_when_recall_non_empty", bool)
    _apply_if_present(cfg, mstack, "identity_path")
    _apply_if_present(cfg, mstack, "wake_char_budget")
    _apply_if_present(cfg, mstack, "recall_char_budget")
    _apply_if_present(cfg, mstack, "recall_n_results")
    _apply_if_present(cfg, mstack, "max_wake_block_chars")
    _apply_if_present(cfg, mstack, "prefer_active_project", None, bool)
    _apply_if_present(cfg, mstack, "use_kg", None, bool)
    _apply_if_present(cfg, mstack, "use_halls", None, bool)
    _apply_if_present(cfg, mstack, "use_closets", None, bool)
    _apply_if_present(cfg, mstack, "follow_tunnels", None, bool)
    _apply_if_present(cfg, mstack, "max_tunnel_hops")
    _apply_if_present(cfg, mstack, "max_tunnel_hits")
    _apply_if_present(cfg, mstack, "avoid_duplicate_session_imports", None, bool)

    graph = g("graph")
    _apply_if_present(cfg, graph, "enabled", "graph_enabled", bool)
    _apply_if_present(cfg, graph, "max_hops", "graph_traverse_max_hops")
    _apply_if_present(cfg, graph, "limit", "graph_traverse_limit")
    _apply_if_present(cfg, graph, "find_tunnels", "graph_find_tunnels", bool)

    # Flat keys
    flat_bools = (
        "enabled", "retrieval_enabled", "extract_facts_each_turn",
        "holographic_enabled", "memory_mirror_enabled",
        "diary_enabled", "diary_read_on_start",
        "aaak_enabled", "aaak_compress_digests",
        "background_ingest", "background_retrieval",
        "memory_stack_enabled", "wake_up_on_session_start",
        "wake_up_on_first_turn", "l2_before_deep_search",
        "l2_skip_deep_search_when_recall_non_empty",
        "graph_enabled", "graph_find_tunnels",
        "dynamics_enabled",
        "fact_extraction_entity_detector",
        "normalize_content",
    )
    for key in flat_bools:
        if key in plugin_config:
            _apply_if_present(cfg, plugin_config, key, key, bool)

    flat_vals = (
        "ingestion_mode", "min_turn_length", "max_turn_length",
        "chunk_size", "chunk_overlap", "target_wing", "target_room", "agent_name",
        "fact_extraction_mode", "min_confidence", "max_facts_per_turn", "allowed_predicates",
        "retrieval_mode", "vector_weight", "bm25_weight", "max_results", "min_score",
        "prioritize_recent_days",
        "include_kg_facts", "kg_entity_limit", "retrieval_timeout_ms",
        "holographic_default_trust",
        "mirror_add", "mirror_replace", "mirror_remove", "mirror_target_wing",
        "diary_agent_name", "diary_wing", "diary_topic", "diary_last_n",
        "aaak_config_path",
        "max_fanout", "prefetch_cache_size", "lexical_scan_limit", "thread_join_timeout_ms",
        "wake_up_wing", "l2_default_room", "identity_path",
        "wake_char_budget", "recall_char_budget", "recall_n_results",
        "max_wake_block_chars", "prefer_active_project",
        "use_kg", "use_halls", "use_closets",
        "max_l3_search_time_ms", "max_recall_chars",
        "max_quote_chars_per_hit", "max_total_quoted_chars",
        "follow_tunnels", "max_tunnel_hops", "max_tunnel_hits",
        "avoid_duplicate_session_imports",
        "graph_traverse_max_hops", "graph_traverse_limit",
        "duplicate_check_enabled", "duplicate_threshold",
        "cache_ttl_seconds",
    )
    for key in flat_vals:
        if key in plugin_config:
            _apply_if_present(cfg, plugin_config, key, key)

    # Path aliases
    _apply_if_present(cfg, plugin_config, "palace_path", "palace_data_dir")
    _apply_if_present(cfg, plugin_config, "palace_data_dir")
    _apply_if_present(cfg, plugin_config, "lib_path", "mempalace_lib_dir")
    _apply_if_present(cfg, plugin_config, "mempalace_lib_dir")


def _load_hermes_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config as _load_config
        loaded = _load_config()
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def load_config(config_data: Optional[Dict[str, Any]] = None) -> MemPalaceConfig:
    """Load configuration from Hermes config + environment.

    ``memory.provider: mempalace`` activates the provider.
    ``HERMES_MEMPALACE_MEMORY_ENABLED=0`` overrides enabled=false.
    All other config flows through normally.
    """
    if config_data is not None:
        plugin_config = _gather_plugin_config(config_data)
        mem = _nested(config_data, "memory")
        if isinstance(mem, dict):
            plugin_config = _merge_plugin_dicts(plugin_config, mem)
            if (
                str(mem.get("provider", "")).strip().lower() == "mempalace"
                and "enabled" not in plugin_config
            ):
                plugin_config["enabled"] = True
    else:
        raw = _load_hermes_config()
        plugin_config = _gather_plugin_config(raw)
        mem = _nested(raw, "memory")
        if isinstance(mem, dict):
            plugin_config = _merge_plugin_dicts(plugin_config, mem)
            if (
                str(mem.get("provider", "")).strip().lower() == "mempalace"
                and "enabled" not in plugin_config
            ):
                plugin_config["enabled"] = True

    env_enabled = os.environ.get("HERMES_MEMPALACE_MEMORY_ENABLED")
    if env_enabled is not None:
        plugin_config = _merge_plugin_dicts(plugin_config, {"enabled": _truthy(env_enabled)})

    # Env path overrides
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