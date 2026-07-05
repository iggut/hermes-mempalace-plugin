"""MemPalace API — thin, fail-open wrapper around native MemPalace operations.

This wrapper keeps Hermes chat running even if MemPalace is unavailable:
all tool handlers catch and serialize errors instead of propagating them.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import MemPalaceConfig

logger = logging.getLogger(__name__)

PALACE_PROTOCOL = """IMPORTANT — MemPalace Memory Protocol:
1. ON WAKE-UP: Call mempalace_status to load palace overview + AAAK spec.
2. BEFORE RESPONDING about any person, project, or past event: call mempalace_kg_query or mempalace_search FIRST. Never guess — verify.
3. IF UNSURE about a fact (name, gender, age, relationship): say \"let me check\" and query the palace. Wrong is worse than slow.
4. AFTER EACH SESSION: call mempalace_diary_write to record what happened, what you learned, what matters.
5. WHEN FACTS CHANGE: call mempalace_kg_invalidate on the old fact, mempalace_kg_add for the new one.

This protocol ensures the AI KNOWS before it speaks. Storage is not memory — but storage + this protocol = memory."""

AAAK_SPEC = """AAAK is a compressed memory dialect that MemPalace uses for efficient storage.
It is designed to be readable by both humans and LLMs without decoding.

FORMAT:
  ENTITIES: 3-letter uppercase codes. ALC=Alice, JOR=Jordan, RIL=Riley, MAX=Max, BEN=Ben.
  EMOTIONS: *action markers* before/during text. *warm*=joy, *fierce*=determined, *raw*=vulnerable, *bloom*=tenderness.
  STRUCTURE: Pipe-separated fields. FAM: family | PROJ: projects | ⚠: warnings/reminders.
  DATES: ISO format (2026-03-31). COUNTS: Nx = N mentions (e.g., 570x).
  IMPORTANCE: ★ to ★★★★★ (1-5 scale).
  HALLS: hall_facts, hall_events, hall_discoveries, hall_preferences, hall_advice.
  WINGS: wing_user, wing_agent, wing_team, wing_code, wing_myproject, wing_hardware, wing_ue5, wing_ai_research.
  ROOMS: Hyphenated slugs representing named ideas (e.g., chromadb-setup, gpu-pricing).

EXAMPLE:
  FAM: ALC→♡JOR | 2D(kids): RIL(18,sports) MAX(11,chess+swimming) | BEN(contributor)

Read AAAK naturally — expand codes mentally, treat *markers* as emotional context.
When WRITING AAAK: use entity codes, mark emotions, keep structure tight."""


def _normalize(s: str) -> str:
    return " ".join(str(s).lower().replace("-", " ").replace("_", " ").split())


class MemPalaceAPI:
    """Thin, fail-open MemPalace operations wrapper."""

    TOOL_SPECS: List[Dict[str, Any]] = [
        {
            "name": "mempalace_status",
            "description": "Palace overview — total drawers, wing and room counts",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "mempalace_list_wings",
            "description": "List all wings with drawer counts",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "mempalace_list_rooms",
            "description": "List rooms within a wing (or all rooms if no wing given)",
            "parameters": {
                "type": "object",
                "properties": {
                    "wing": {"type": "string", "description": "Wing to list rooms for (optional)"}
                },
            },
        },
        {
            "name": "mempalace_get_taxonomy",
            "description": "Full taxonomy: wing → room → drawer count",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "mempalace_get_aaak_spec",
            "description": "Get the AAAK dialect specification — the compressed memory format MemPalace uses. Call this if you need to read or write AAAK-compressed memories.",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "mempalace_kg_query",
            "description": "Query the knowledge graph for an entity's relationships. Returns typed facts with temporal validity. Filter by date with as_of to see what was true at a point in time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string", "description": "Entity to query"},
                    "as_of": {"type": "string", "description": "Date/datetime filter (optional)"},
                    "direction": {"type": "string", "description": "outgoing, incoming, or both (default: both)"},
                },
                "required": ["entity"],
            },
        },
        {
            "name": "mempalace_kg_add",
            "description": "Add a fact to the knowledge graph. Subject → predicate → object with optional time window.",
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "The entity doing/being something"},
                    "predicate": {"type": "string", "description": "Relationship type"},
                    "object": {"type": "string", "description": "Connected entity"},
                    "valid_from": {"type": "string", "description": "When this became true (optional)"},
                    "valid_to": {"type": "string", "description": "When this stopped being true (optional)"},
                    "source_closet": {"type": "string", "description": "Closet ID where this fact appears (optional)"},
                    "source_file": {"type": "string", "description": "Source file path (optional)"},
                    "source_drawer_id": {"type": "string", "description": "Drawer ID the fact was extracted from (optional)"},
                },
                "required": ["subject", "predicate", "object"],
            },
        },
        {
            "name": "mempalace_kg_invalidate",
            "description": "Mark a fact as no longer true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "Entity"},
                    "predicate": {"type": "string", "description": "Relationship"},
                    "object": {"type": "string", "description": "Connected entity"},
                    "ended": {"type": "string", "description": "When it stopped being true (optional; default today)"},
                },
                "required": ["subject", "predicate", "object"],
            },
        },
        {
            "name": "mempalace_kg_timeline",
            "description": "Chronological timeline of facts. Shows the story of an entity (or everything) in order.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string", "description": "Entity to get timeline for (optional)"}
                },
            },
        },
        {
            "name": "mempalace_kg_stats",
            "description": "Knowledge graph overview: entities, triples, current vs expired facts, relationship types.",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "mempalace_traverse",
            "description": "Walk the palace graph from a room. Shows connected ideas across wings — the tunnels.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_room": {"type": "string", "description": "Room to start from"},
                    "max_hops": {"type": "integer", "description": "How many connections to follow (default: 2)"},
                },
                "required": ["start_room"],
            },
        },
        {
            "name": "mempalace_find_tunnels",
            "description": "Find rooms that bridge two wings — the hallways connecting different domains.",
            "parameters": {
                "type": "object",
                "properties": {
                    "wing_a": {"type": "string", "description": "First wing (optional)"},
                    "wing_b": {"type": "string", "description": "Second wing (optional)"},
                },
            },
        },
        {
            "name": "mempalace_graph_stats",
            "description": "Palace graph overview: total rooms, tunnel connections, edges between wings.",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "mempalace_create_tunnel",
            "description": "Create a cross-wing tunnel linking two palace locations.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_wing": {"type": "string", "description": "Wing of the source"},
                    "source_room": {"type": "string", "description": "Room in the source wing"},
                    "target_wing": {"type": "string", "description": "Wing of the target"},
                    "target_room": {"type": "string", "description": "Room in the target wing"},
                    "label": {"type": "string", "description": "Description of the connection"},
                    "source_drawer_id": {"type": "string", "description": "Optional specific drawer ID"},
                    "target_drawer_id": {"type": "string", "description": "Optional specific drawer ID"},
                },
                "required": ["source_wing", "source_room", "target_wing", "target_room"],
            },
        },
        {
            "name": "mempalace_list_tunnels",
            "description": "List all explicit cross-wing tunnels. Optionally filter by wing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "wing": {"type": "string", "description": "Filter tunnels by wing (optional)"}
                },
            },
        },
        {
            "name": "mempalace_delete_tunnel",
            "description": "Delete an explicit tunnel by its ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tunnel_id": {"type": "string", "description": "Tunnel ID to delete"}
                },
                "required": ["tunnel_id"],
            },
        },
        {
            "name": "mempalace_follow_tunnels",
            "description": "Follow tunnels from a room to see what it connects to in other wings.",
            "parameters": {
                "type": "object",
                "properties": {
                    "wing": {"type": "string", "description": "Wing to start from"},
                    "room": {"type": "string", "description": "Room to follow tunnels from"},
                },
                "required": ["wing", "room"],
            },
        },
        {
            "name": "mempalace_search",
            "description": "Semantic search. Returns verbatim drawer content with similarity scores.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Short search query ONLY — keywords or a question.", "maxLength": 250},
                    "limit": {"type": "integer", "description": "Max results (default 5)", "minimum": 1, "maximum": 100},
                    "wing": {"type": "string", "description": "Filter by wing (optional)"},
                    "room": {"type": "string", "description": "Filter by room (optional)"},
                    "max_distance": {"type": "number", "description": "Max cosine distance threshold (default 1.5; 0 disables)"},
                    "context": {"type": "string", "description": "Background context for the search (optional)"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "mempalace_check_duplicate",
            "description": "Check if content already exists in the palace before filing",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Content to check"},
                    "threshold": {"type": "number", "description": "Similarity threshold 0-1 (default 0.9)"},
                },
                "required": ["content"],
            },
        },
        {
            "name": "mempalace_add_drawer",
            "description": "File verbatim content into the palace. Checks for duplicates first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "wing": {"type": "string", "description": "Wing (project name)"},
                    "room": {"type": "string", "description": "Room (aspect: backend, decisions, meetings...)"},
                    "content": {"type": "string", "description": "Verbatim content to store — exact words, never summarized"},
                    "source_file": {"type": "string", "description": "Where this came from (optional)"},
                    "added_by": {"type": "string", "description": "Who is filing this (default: hermes)"},
                },
                "required": ["wing", "room", "content"],
            },
        },
        {
            "name": "mempalace_delete_drawer",
            "description": "Delete a drawer by ID. Irreversible.",
            "parameters": {
                "type": "object",
                "properties": {
                    "drawer_id": {"type": "string", "description": "ID of the drawer to delete"}
                },
                "required": ["drawer_id"],
            },
        },
        {
            "name": "mempalace_sync",
            "description": "Prune drawers whose source files are gitignored, deleted, or moved. Returns dry-run report by default; pass apply=true to commit deletions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_dir": {"type": "string", "description": "Project root to scope the sync (optional)"},
                    "wing": {"type": "string", "description": "Limit to one wing (optional)"},
                    "apply": {"type": "boolean", "description": "Actually delete drawers; default is dry-run preview"},
                },
            },
        },
        {
            "name": "mempalace_get_drawer",
            "description": "Fetch a single drawer by ID — returns full content and metadata.",
            "parameters": {
                "type": "object",
                "properties": {
                    "drawer_id": {"type": "string", "description": "ID of the drawer to fetch"}
                },
                "required": ["drawer_id"],
            },
        },
        {
            "name": "mempalace_list_drawers",
            "description": "List drawers with pagination. Optional wing/room filter. Returns IDs, wings, rooms, content previews, and total matching count for pagination.",
            "parameters": {
                "type": "object",
                "properties": {
                    "wing": {"type": "string", "description": "Filter by wing (optional)"},
                    "room": {"type": "string", "description": "Filter by room (optional)"},
                    "limit": {"type": "integer", "description": "Max results per page (default 20, max 100)", "minimum": 1, "maximum": 100},
                    "offset": {"type": "integer", "description": "Offset for pagination (default 0)", "minimum": 0},
                },
            },
        },
        {
            "name": "mempalace_update_drawer",
            "description": "Update an existing drawer's content and/or metadata (wing, room). Fetches existing drawer first; returns error if not found.",
            "parameters": {
                "type": "object",
                "properties": {
                    "drawer_id": {"type": "string", "description": "ID of the drawer to update"},
                    "content": {"type": "string", "description": "New content (optional — omit to keep existing)"},
                    "wing": {"type": "string", "description": "New wing (optional — omit to keep existing)"},
                    "room": {"type": "string", "description": "New room (optional — omit to keep existing)"},
                },
                "required": ["drawer_id"],
            },
        },
        {
            "name": "mempalace_diary_write",
            "description": "Write to your personal agent diary in AAAK format. Each agent has their own diary with full history.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string", "description": "Your name — each agent gets their own diary wing"},
                    "entry": {"type": "string", "description": "Your diary entry in AAAK format"},
                    "topic": {"type": "string", "description": "Topic tag (optional, default: general)"},
                    "wing": {"type": "string", "description": "Target wing for this diary entry (optional)"},
                },
                "required": ["agent_name", "entry"],
            },
        },
        {
            "name": "mempalace_diary_read",
            "description": "Read your recent diary entries (in AAAK). See what past versions of yourself recorded — your journal across sessions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string", "description": "Your name — each agent gets their own diary wing"},
                    "last_n": {"type": "integer", "description": "Number of recent entries to read (default: 10)"},
                    "wing": {"type": "string", "description": "Wing to read diary entries from (optional)"},
                },
                "required": ["agent_name"],
            },
        },
        {
            "name": "mempalace_hook_settings",
            "description": "Get or set hook behavior. silent_save: True = save directly, False = legacy blocking. desktop_toast: True = show desktop notification. Call with no args to view.",
            "parameters": {
                "type": "object",
                "properties": {
                    "silent_save": {"type": "boolean", "description": "True = silent direct save, False = blocking MCP calls"},
                    "desktop_toast": {"type": "boolean", "description": "True = show desktop toast via notify-send"},
                },
            },
        },
        {
            "name": "mempalace_memories_filed_away",
            "description": "Check if a recent palace checkpoint was saved. Returns message count and timestamp.",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "mempalace_reconnect",
            "description": "Force reconnect to the palace database. Use after external scripts or CLI commands modified the palace directly, which can leave the in-memory index stale.",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "mempalace_dynamics_apply",
            "description": (
                "Apply Ebbinghaus exponential decay to all hall and tunnel connection "
                "strengths based on time since last activation. Higher stability = "
                "slower decay (Cepeda spacing effect). Pure admin operation — call "
                "this periodically (e.g. via cron) or before large prefetch batches. "
                "Returns the count of records touched, mean strength before/after, "
                "and the timestamp used."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "wing": {
                        "type": "string",
                        "description": "Optional wing filter — only decay halls in this wing + tunnels touching it. Default: all wings.",
                    },
                    "now": {
                        "type": "string",
                        "description": "Optional ISO-8601 timestamp for deterministic decay (testing). Default: current UTC.",
                    },
                },
            },
        },
        {
            "name": "mempalace_potentiate",
            "description": (
                "Strengthen a hall or tunnel connection on a co-access event "
                "(Hebbian potentiation). Updates strength (capped at MAX_STRENGTH=5.0), "
                "last_activated, access_count, and grows stability if the gap since the "
                "prior activation is at least 1 hour (the Cepeda spacing effect). "
                "Use this from the retrieval path: every time a connection is surfaced "
                "in a recall block, the system reinforces it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "connection_id": {
                        "type": "string",
                        "description": "Hall or tunnel record ID to potentiate.",
                    },
                    "kind": {
                        "type": "string",
                        "description": "'hall' or 'tunnel'. Default: 'tunnel'.",
                    },
                    "increment": {
                        "type": "number",
                        "description": "Strength increment. Default: 0.05 (POTENTIATION_INCREMENT).",
                    },
                },
                "required": ["connection_id"],
            },
        },
        {
            "name": "mempalace_repair_scan",
            "description": "Scan palace for corruption, inconsistencies, or missing metadata. Returns a diagnostic report.",
            "parameters": {
                "type": "object",
                "properties": {
                    "wing": {
                        "type": "string",
                        "description": "Limit scan to one wing (optional)",
                    },
                },
            },
        },
        {
            "name": "mempalace_repair_prune",
            "description": "Remove corrupt or orphaned drawers from the palace. Returns count of pruned items.",
            "parameters": {
                "type": "object",
                "properties": {
                    "confirm": {
                        "type": "boolean",
                        "description": "Actually prune (default: dry-run preview)",
                    },
                },
            },
        },
        {
            "name": "mempalace_export",
            "description": "Export palace data to markdown or JSON format. Returns the output directory path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "output_dir": {
                        "type": "string",
                        "description": "Directory to write export files",
                    },
                    "format": {
                        "type": "string",
                        "description": "Export format: markdown or json (default: markdown)",
                    },
                },
                "required": ["output_dir"],
            },
        },
        {
            "name": "mempalace_dedup_stats",
            "description": "Show deduplication statistics — how many near-duplicate drawers exist in the palace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "wing": {
                        "type": "string",
                        "description": "Limit to one wing (optional)",
                    },
                },
            },
        },
        {
            "name": "mempalace_dedup_run",
            "description": "Run deduplication — merge or remove near-duplicate drawers. Returns count of duplicates found/removed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "wing": {
                        "type": "string",
                        "description": "Limit to one wing (optional)",
                    },
                    "threshold": {
                        "type": "number",
                        "description": "Similarity threshold 0-1 (default 0.95)",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "Preview only (default true)",
                    },
                },
            },
        },
        {
            "name": "mempalace_detect_entities",
            "description": "Detect entities (people, projects, organizations) in text using MemPalace entity detection.\nUses extract_candidates + classify_entity for text-based detection (v3.5.0 compatible).",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Text to analyze for entities",
                    },
                    "languages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Languages to detect (default: [en])",
                    },
                },
                "required": ["text"],
            },
        },
        {
            "name": "mempalace_list_hallways",
            "description": "List within-wing hallway records (entity-to-entity co-occurrence connections with Hebbian strength). Optionally filter by wing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "wing": {
                        "type": "string",
                        "description": "Wing to filter hallways by (optional)",
                    },
                },
            },
        },
        {
            "name": "mempalace_delete_hallway",
            "description": "Delete a hallway record by its ID. Returns {deleted: bool}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "hallway_id": {
                        "type": "string",
                        "description": "Hallway record ID to delete",
                    },
                },
                "required": ["hallway_id"],
            },
        },
    ]

    def __init__(
        self,
        palace_data_dir: str = "",
        mempalace_lib_dir: str = "",
        config: Optional[MemPalaceConfig] = None,
        metric_fn: Optional[Callable[[str], None]] = None,
    ):
        self._palace_data_dir = palace_data_dir or ""
        self._mempalace_lib_dir = mempalace_lib_dir or ""
        self._config = config or MemPalaceConfig()
        self._metric = metric_fn or (lambda _name: None)

        self._imported = False
        self._import_error: Optional[str] = None

        self._search_memories_fn: Any = None
        self._get_collection_fn: Any = None
        self._miner_add_drawer_fn: Any = None
        self._chunk_text_fn: Any = None
        self._status_fn: Any = None
        self._sync_palace_fn: Any = None
        self._traverse_fn: Any = None
        self._find_tunnels_fn: Any = None
        self._graph_stats_fn: Any = None
        self._create_tunnel_fn: Any = None
        self._list_tunnels_fn: Any = None
        self._delete_tunnel_fn: Any = None
        self._follow_tunnels_fn: Any = None
        self._knowledge_graph_cls: Any = None
        self._sanitize_name_fn: Any = None
        self._sanitize_content_fn: Any = None
        self._sanitize_kg_value_fn: Any = None
        self._sanitize_iso_temporal_fn: Any = None
        self._native_config_cls: Any = None
        self._shared_system_client: Any = None

        # Dynamics (Hebb/Ebbinghaus/Cepeda) — set by _ensure_imported.
        self._apply_decay_fn: Any = None
        self._potentiate_fn: Any = None
        self._initialize_dynamics_fn: Any = None
        # Persistence helpers for halls + tunnels (for decay/potentiate).
        self._load_halls_fn: Any = None
        self._save_halls_fn: Any = None
        self._load_tunnels_fn: Any = None
        self._save_tunnels_fn: Any = None

        # Repair, export, dedup, entity detection — set by _ensure_imported.
        self._repair_scan_fn: Any = None
        self._repair_prune_fn: Any = None
        self._export_palace_fn: Any = None
        self._dedup_stats_fn: Any = None
        self._dedup_run_fn: Any = None
        self._detect_entities_fn: Any = None
        # v3.5.0: text-based entity detection components
        self._extract_candidates_fn: Any = None
        self._classify_entity_fn: Any = None
        self._confirm_entities_fn: Any = None
        # v3.5.0: hallways management
        self._list_hallways_fn: Any = None
        self._delete_hallway_fn: Any = None

        self._col: Any = None
        self._kg: Any = None

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

        # Package shadowing fix: when the plugin directory is on sys.path
        # (standalone test mode), Python resolves `import mempalace` to the
        # plugin package, not the library. We need to:
        # 1. Move the library path to the front of sys.path
        # 2. Evict any cached `mempalace` modules that point at the plugin
        # 3. Import library functions (they bind to the library code)
        # 4. Restore the original sys.path and plugin modules afterward
        _evicted: dict = {}
        _plugin_path = None
        if lib:
            lib_lp = str(Path(lib).expanduser())
            # Find and temporarily remove the plugins dir from sys.path
            for _i, _p in enumerate(sys.path):
                if _p.endswith(".hermes/plugins") or _p.endswith(".hermes/plugins/"):
                    _plugin_path = sys.path.pop(_i)
                    break
            # Ensure library path is at position 0
            if lib_lp in sys.path:
                sys.path.remove(lib_lp)
            sys.path.insert(0, lib_lp)
            # Evict cached mempalace modules that aren't from the library
            lib_realpath = str(Path(lib).resolve() / "mempalace")
            for _mod_name in list(sys.modules):
                if _mod_name == "mempalace" or _mod_name.startswith("mempalace."):
                    _mod = sys.modules.get(_mod_name)
                    _mod_file = getattr(_mod, "__file__", "") or ""
                    try:
                        _resolved = str(Path(_mod_file).resolve())
                    except Exception:
                        _resolved = _mod_file
                    if lib_realpath not in _resolved:
                        _evicted[_mod_name] = sys.modules.pop(_mod_name)

        try:
            from mempalace.searcher import search_memories as _sm
            self._search_memories_fn = _sm
        except Exception as e:
            logger.debug("[MemPalaceAPI] searcher import failed: %s", e)

        try:
            from mempalace.palace import get_collection as _gc
            self._get_collection_fn = _gc
        except Exception as e:
            logger.debug("[MemPalaceAPI] palace import failed: %s", e)

        try:
            from mempalace.miner import add_drawer as _mad
            from mempalace.miner import chunk_text as _chunk
            self._miner_add_drawer_fn = _mad
            self._chunk_text_fn = _chunk
        except Exception as e:
            logger.debug("[MemPalaceAPI] miner import failed: %s", e)

        try:
            from mempalace.repair import status as _status
            self._status_fn = _status
        except Exception as e:
            logger.debug("[MemPalaceAPI] repair.status import failed: %s", e)

        try:
            from mempalace.sync import sync_palace as _sync_palace
            self._sync_palace_fn = _sync_palace
        except Exception as e:
            logger.debug("[MemPalaceAPI] sync import failed: %s", e)

        try:
            from mempalace.palace_graph import create_tunnel as _create_tunnel
            from mempalace.palace_graph import delete_tunnel as _delete_tunnel
            from mempalace.palace_graph import find_tunnels as _find_tunnels
            from mempalace.palace_graph import follow_tunnels as _follow_tunnels
            from mempalace.palace_graph import graph_stats as _graph_stats
            from mempalace.palace_graph import list_tunnels as _list_tunnels
            from mempalace.palace_graph import traverse as _traverse

            self._traverse_fn = _traverse
            self._find_tunnels_fn = _find_tunnels
            self._graph_stats_fn = _graph_stats
            self._create_tunnel_fn = _create_tunnel
            self._list_tunnels_fn = _list_tunnels
            self._delete_tunnel_fn = _delete_tunnel
            self._follow_tunnels_fn = _follow_tunnels
        except Exception as e:
            logger.debug("[MemPalaceAPI] palace_graph import failed: %s", e)

        try:
            from mempalace.knowledge_graph import KnowledgeGraph as _KG
            self._knowledge_graph_cls = _KG
        except Exception as e:
            logger.debug("[MemPalaceAPI] knowledge_graph import failed: %s", e)

        try:
            from mempalace.config import MempalaceConfig as _NativeConfig
            from mempalace.config import sanitize_content as _sanitize_content
            from mempalace.config import sanitize_iso_temporal as _sanitize_iso_temporal
            from mempalace.config import sanitize_kg_value as _sanitize_kg_value
            from mempalace.config import sanitize_name as _sanitize_name

            self._native_config_cls = _NativeConfig
            self._sanitize_name_fn = _sanitize_name
            self._sanitize_content_fn = _sanitize_content
            self._sanitize_kg_value_fn = _sanitize_kg_value
            self._sanitize_iso_temporal_fn = _sanitize_iso_temporal
        except Exception as e:
            logger.debug("[MemPalaceAPI] config sanitizers import failed: %s", e)

        try:
            from chromadb.api.client import SharedSystemClient as _SharedSystemClient
            self._shared_system_client = _SharedSystemClient
        except Exception:
            self._shared_system_client = None

        # Dynamics (Hebbian potentiation + Ebbinghaus decay) and the
        # persistence helpers for halls + tunnels. The dynamics functions
        # are pure (no I/O) — the persistence helpers are module-level
        # functions inside hallways / palace_graph that read/write JSON.
        try:
            from mempalace.dynamics import (
                apply_decay as _apply_decay,
                potentiate as _potentiate,
                initialize_dynamics_fields as _init_dyn,
            )
            self._apply_decay_fn = _apply_decay
            self._potentiate_fn = _potentiate
            self._initialize_dynamics_fn = _init_dyn
        except Exception as e:
            logger.debug("[MemPalaceAPI] dynamics import failed: %s", e)

        try:
            from mempalace.hallways import (
                _load_hallways as _load_halls,
                _save_hallways as _save_halls,
            )
            self._load_halls_fn = _load_halls
            self._save_halls_fn = _save_halls
        except Exception as e:
            logger.debug("[MemPalaceAPI] hallways persistence import failed: %s", e)

        try:
            from mempalace.palace_graph import (
                _load_tunnels as _load_tunnels,
                _save_tunnels as _save_tunnels,
            )
            self._load_tunnels_fn = _load_tunnels
            self._save_tunnels_fn = _save_tunnels
        except Exception as e:
            logger.debug("[MemPalaceAPI] palace_graph persistence import failed: %s", e)

        try:
            from mempalace.repair import scan_palace as _repair_scan
            self._repair_scan_fn = _repair_scan
        except Exception as e:
            logger.debug("[MemPalaceAPI] repair.scan_palace import failed: %s", e)

        try:
            from mempalace.repair import prune_corrupt as _repair_prune
            self._repair_prune_fn = _repair_prune
        except Exception as e:
            logger.debug("[MemPalaceAPI] repair.prune_corrupt import failed: %s", e)

        try:
            from mempalace.exporter import export_palace as _export_palace
            self._export_palace_fn = _export_palace
        except Exception as e:
            logger.debug("[MemPalaceAPI] exporter.export_palace import failed: %s", e)

        try:
            from mempalace.dedup import show_stats as _dedup_stats
            self._dedup_stats_fn = _dedup_stats
        except Exception as e:
            logger.debug("[MemPalaceAPI] dedup.show_stats import failed: %s", e)

        try:
            from mempalace.dedup import dedup_palace as _dedup_run
            self._dedup_run_fn = _dedup_run
        except Exception as e:
            logger.debug("[MemPalaceAPI] dedup.dedup_palace import failed: %s", e)

        # entity_detector.detect_entities changed in v3.5.0: it now takes
        # file_paths (list[Path]) instead of text (str). The plugin's
        # tool_detect_entities works on text, so we import the lower-level
        # text-based functions (extract_candidates + classify_entity +
        # confirm_entities) and build a text wrapper in _detect_entities_fn.
        try:
            from mempalace.entity_detector import (
                extract_candidates as _extract_candidates,
                classify_entity as _classify_entity,
                confirm_entities as _confirm_entities,
            )
            self._extract_candidates_fn = _extract_candidates
            self._classify_entity_fn = _classify_entity
            self._confirm_entities_fn = _confirm_entities
            # Keep the raw detect_entities for file-path mode
            try:
                from mempalace.entity_detector import detect_entities as _detect_entities
                self._detect_entities_fn = _detect_entities
            except Exception:
                self._detect_entities_fn = None
        except Exception as e:
            logger.debug("[MemPalaceAPI] entity_detector import failed: %s", e)
            self._extract_candidates_fn = None
            self._classify_entity_fn = None
            self._confirm_entities_fn = None

        # New 3.5.0: hallways management
        try:
            from mempalace.hallways import list_hallways as _list_hallways
            from mempalace.hallways import delete_hallway as _delete_hallway
            self._list_hallways_fn = _list_hallways
            self._delete_hallway_fn = _delete_hallway
        except Exception as e:
            logger.debug("[MemPalaceAPI] hallways management import failed: %s", e)
            self._list_hallways_fn = None
            self._delete_hallway_fn = None

        self._imported = bool(self._get_collection_fn)
        if not self._imported:
            self._import_error = "No mempalace modules could be imported"
            logger.warning("[MemPalaceAPI] mempalace import failed — no modules available")
        else:
            self._import_error = None

        # Restore: put the plugins path back and re-instate evicted modules
        # so the plugin's own code continues to work after library import.
        # Library functions are already bound to self._*_fn attributes.
        if _plugin_path is not None and _plugin_path not in sys.path:
            sys.path.insert(0, _plugin_path)
        for _mod_name, _mod in _evicted.items():
            if _mod_name not in sys.modules:
                sys.modules[_mod_name] = _mod

    @property
    def is_available(self) -> bool:
        if not self._palace_data_dir or not Path(self._palace_data_dir).exists():
            return False
        self._ensure_imported()
        return bool(self._imported)

    def _sanitize_name(self, value: str, field_name: str = "name") -> str:
        if self._sanitize_name_fn is None:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} is required")
            return value.strip()
        return self._sanitize_name_fn(value, field_name)

    def _sanitize_optional_name(self, value: Optional[str], field_name: str = "name") -> Optional[str]:
        if value in (None, ""):
            return None
        return self._sanitize_name(value, field_name)

    def _sanitize_content(self, value: str) -> str:
        if self._sanitize_content_fn is None:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("content is required")
            return value
        return self._sanitize_content_fn(value)

    def _sanitize_kg_value(self, value: str, field_name: str = "value") -> str:
        if self._sanitize_kg_value_fn is None:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} is required")
            return value.strip()
        return self._sanitize_kg_value_fn(value, field_name)

    def _sanitize_iso_temporal(self, value: Optional[str], field_name: str = "date") -> Optional[str]:
        if self._sanitize_iso_temporal_fn is None:
            return value
        return self._sanitize_iso_temporal_fn(value, field_name)

    def _result_field(self, result: Any, key: str) -> Any:
        if isinstance(result, dict):
            return result.get(key)
        return getattr(result, key, None)

    def _collection(self, create: bool = False) -> Any:
        if self._col is not None:
            return self._col
        self._ensure_imported()
        if self._get_collection_fn is None:
            return None
        try:
            self._col = self._get_collection_fn(self._palace_data_dir, create=create)
        except Exception as e:
            logger.debug("[MemPalaceAPI] get_collection failed: %s", e)
            return None
        return self._col

    def _safe_meta(self, meta: Any) -> Dict[str, Any]:
        return dict(meta or {})

    def _build_where(self, wing: Optional[str] = None, room: Optional[str] = None, extra: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
        conditions: List[Dict[str, Any]] = []
        if wing:
            conditions.append({"wing": wing})
        if room:
            conditions.append({"room": room})
        if extra:
            conditions.extend(extra)
        if not conditions:
            return None
        if len(conditions) == 1:
            return conditions[0]
        return {"$and": conditions}

    def _fetch_metadata(self, where: Optional[Dict[str, Any]] = None, batch_size: int = 1000) -> List[Dict[str, Any]]:
        col = self._collection()
        if col is None:
            return []
        batch_size = max(1, min(int(batch_size), 5000))
        all_meta: List[Dict[str, Any]] = []
        offset = 0
        while True:
            kwargs: Dict[str, Any] = {"include": ["metadatas"], "limit": batch_size, "offset": offset}
            if where:
                kwargs["where"] = where
            result = col.get(**kwargs)
            metadatas = self._result_field(result, "metadatas") or []
            if not metadatas:
                break
            all_meta.extend(self._safe_meta(m) for m in metadatas)
            if len(metadatas) < batch_size:
                break
            offset += len(metadatas)
        return all_meta

    def _count_rows(self, where: Optional[Dict[str, Any]] = None, batch_size: int = 5000) -> int:
        col = self._collection()
        if col is None:
            return 0
        if where is None:
            try:
                return int(col.count())
            except Exception:
                return 0
        batch_size = max(1, min(int(batch_size), 10000))
        total = 0
        offset = 0
        while True:
            kwargs: Dict[str, Any] = {"limit": batch_size, "offset": offset}
            if where:
                kwargs["where"] = where
            result = col.get(**kwargs)
            ids = self._result_field(result, "ids") or []
            if not ids:
                break
            total += len(ids)
            if len(ids) < batch_size:
                break
            offset += len(ids)
        return total

    def _resolve_kg(self) -> Any:
        if self._kg is not None:
            return self._kg
        self._ensure_imported()
        if self._knowledge_graph_cls is None:
            return None
        try:
            db_path = None
            if self._palace_data_dir:
                db_path = str(Path(self._palace_data_dir).parent / "knowledge_graph.sqlite3")
            self._kg = self._knowledge_graph_cls(db_path=db_path)
        except Exception as e:
            logger.warning("[MemPalaceAPI] KnowledgeGraph unavailable: %s", e)
            self._kg = None
        return self._kg

    def _upsert(self, col: Any, *, ids: List[str], documents: List[str], metadatas: List[Dict[str, Any]]) -> None:
        if hasattr(col, "upsert"):
            col.upsert(ids=ids, documents=documents, metadatas=metadatas)
        else:
            col.add(ids=ids, documents=documents, metadatas=metadatas)

    def _fallback_drawer_id(self, wing: str, room: str, content: str) -> str:
        return f"drawer_{wing}_{room}_{hashlib.sha256((wing + room + content).encode()).hexdigest()[:24]}"

    def _check_duplicate(self, content: str, col: Any, threshold: Optional[float] = None) -> Optional[str]:
        if not self._config.duplicate_check_enabled:
            return None
        similarity_threshold = float(threshold if threshold is not None else self._config.duplicate_threshold)
        try:
            result = col.query(query_texts=[content], n_results=1, include=["distances"])
            distances = self._result_field(result, "distances") or [[]]
            if distances and distances[0] and (1.0 - float(distances[0][0])) >= similarity_threshold:
                ids = self._result_field(result, "ids") or [[]]
                if ids and ids[0]:
                    return str(ids[0][0])
        except Exception as e:
            logger.debug("[MemPalaceAPI] duplicate check failed: %s", e)
        return None

    def search(
        self,
        query: str,
        *,
        wing: str = "",
        room: str = "",
        limit: int = 8,
        min_score: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Search the palace. If ``min_score`` is None, use config default.

        Pass a custom ``min_score`` (e.g. 0.3 for vague NL asks) to override.
        """
        effective_min = cfg_min = float(self._config.min_score) if self._config else 0.3
        if min_score is not None:
            effective_min = float(min_score)
        if not query:
            return []
        semantic = self._semantic_search(query, wing=wing, room=room, limit=limit, min_score=effective_min)
        if len(semantic) < limit:
            lexical = self._lexical_fallback(query, limit=limit - len(semantic))
            seen_ids = {r["drawer_id"] for r in semantic}
            for r in lexical:
                if r["drawer_id"] not in seen_ids:
                    semantic.append(r)
                    seen_ids.add(r["drawer_id"])
        return semantic

    def _semantic_search(self, query: str, wing: str = "", room: str = "", limit: int = 8, min_score: float = 0.3) -> List[Dict[str, Any]]:
        self._ensure_imported()
        search_fn = self._search_memories_fn
        if hasattr(search_fn, "search_memories"):
            search_fn = search_fn.search_memories
        if search_fn is None:
            return []
        max_dist = max(0.0, 1.0 - float(min_score)) if min_score > 0 else 0.0
        try:
            raw = search_fn(
                query=query,
                palace_path=self._palace_data_dir,
                wing=wing or None,
                room=room or None,
                n_results=limit,
                max_distance=max_dist,
            )
        except TypeError:
            try:
                raw = search_fn(query, self._palace_data_dir, wing=wing or None, room=room or None, n_results=limit, max_distance=max_dist)
            except Exception as e:
                logger.debug("[MemPalaceAPI] search_memories failed: %s", e)
                raw = None
        except Exception as e:
            logger.debug("[MemPalaceAPI] search_memories failed: %s", e)
            raw = None

        if isinstance(raw, dict) and raw.get("error"):
            return []
        results = []
        for hit in (raw or {}).get("results") or []:
            similarity = float(hit.get("similarity", 0.0))
            if similarity < min_score:
                continue
            results.append(
                {
                    "content": hit.get("text") or hit.get("content", ""),
                    "score": similarity,
                    "wing": hit.get("wing", "?"),
                    "room": hit.get("room", "?"),
                    "source_file": hit.get("source_file", "?"),
                    "drawer_id": hit.get("drawer_id", ""),
                    "match_type": str(hit.get("matched_via", "semantic")),
                }
            )
        return results

    def _lexical_fallback(self, query: str, limit: int = 4) -> List[Dict[str, Any]]:
        if not query:
            return []
        col = self._collection()
        if col is None:
            return []

        results: List[Dict[str, Any]] = []
        norm = _normalize(query)

        if len(results) < limit:
            try:
                hit = col.get(ids=[query.strip()])
                ids = self._result_field(hit, "ids") or []
                if ids:
                    metas = self._result_field(hit, "metadatas") or [{}]
                    docs = self._result_field(hit, "documents") or [""]
                    meta = self._safe_meta(metas[0] if metas else {})
                    results.append(
                        {
                            "content": docs[0] if docs else "",
                            "score": 1.0,
                            "wing": meta.get("wing", "?"),
                            "room": meta.get("room", "?"),
                            "source_file": meta.get("source_file", "?"),
                            "drawer_id": ids[0],
                            "match_type": "lexical:id",
                        }
                    )
            except Exception as e:
                logger.debug("[MemPalaceAPI] lexical drawer-id lookup failed: %s", e)

        if len(results) < limit:
            parts = norm.replace(" ", "-").split("-")
            for i in range(len(parts)):
                variant = "_".join(parts[i:])
                if len(variant) < 3:
                    continue
                try:
                    hit = col.get(ids=[f"drawer_{variant}"])
                    ids = self._result_field(hit, "ids") or []
                    if ids:
                        metas = self._result_field(hit, "metadatas") or [{}]
                        docs = self._result_field(hit, "documents") or [""]
                        meta = self._safe_meta(metas[0] if metas else {})
                        results.append(
                            {
                                "content": docs[0] if docs else "",
                                "score": 0.9,
                                "wing": meta.get("wing", "?"),
                                "room": meta.get("room", "?"),
                                "source_file": meta.get("source_file", "?"),
                                "drawer_id": ids[0],
                                "match_type": "lexical:variant",
                            }
                        )
                        break
                except Exception:
                    pass

        if len(results) < limit:
            try:
                scan_limit = getattr(self._config, "lexical_scan_limit", 1000)
                all_results = col.get(limit=scan_limit)
                ids = self._result_field(all_results, "ids") or []
                docs = self._result_field(all_results, "documents") or []
                metas = self._result_field(all_results, "metadatas") or []
                for i, did in enumerate(ids):
                    if len(results) >= limit:
                        break
                    meta = self._safe_meta(metas[i] if i < len(metas) else {})
                    doc = docs[i] if i < len(docs) else ""
                    sf = meta.get("source_file", "")
                    w = meta.get("wing", "")
                    r = meta.get("room", "")
                    if (
                        norm in _normalize(sf)
                        or norm in _normalize(doc[:100])
                        or _normalize(w) in norm
                        or _normalize(r) in norm
                    ):
                        results.append(
                            {
                                "content": doc[:300],
                                "score": 0.7,
                                "wing": w,
                                "room": r,
                                "source_file": sf,
                                "drawer_id": did if isinstance(did, str) else str(i),
                                "match_type": "lexical:meta",
                            }
                        )
            except Exception as e:
                logger.debug("[MemPalaceAPI] lexical meta scan failed: %s", e)

        return results

    def add_drawer(
        self,
        content: str,
        *,
        wing: str = "memory",
        room: str = "conversations",
        source_file: str = "",
        agent: str = "",
        duplicate_threshold: Optional[float] = None,
    ) -> str:
        col = self._collection(create=True)
        if col is None:
            return self._fallback_drawer_id(wing, room, content)

        existing = self._check_duplicate(content, col, threshold=duplicate_threshold)
        if existing:
            self._metric("duplicate_hits")
            return existing
        if self._config.duplicate_check_enabled:
            self._metric("duplicate_misses")

        return self._write_drawer(col, content, wing, room, source_file, agent)

    def _write_drawer(self, col: Any, content: str, wing: str, room: str, source_file: str, agent: str) -> str:
        drawer_id = self._fallback_drawer_id(wing, room, content)
        src = source_file or "inline.md"
        try:
            miner_add = getattr(self._miner_add_drawer_fn, "add_drawer", self._miner_add_drawer_fn)
            if miner_add:
                result = miner_add(col, wing, room, content, src, 0, agent or "hermes")
                if isinstance(result, dict) and result.get("drawer_id"):
                    drawer_id = str(result["drawer_id"])
            else:
                self._upsert(
                    col,
                    ids=[drawer_id],
                    documents=[content],
                    metadatas=[
                        {
                            "wing": wing,
                            "room": room,
                            "source_file": src,
                            "agent": agent or "hermes",
                            "chunk_index": 0,
                        }
                    ],
                )
        except Exception as e:
            logger.debug("[MemPalaceAPI] _write_drawer failed: %s", e)
        else:
            self._metric("chunk_writes")
        return drawer_id

    def chunk_and_add(
        self,
        content: str,
        *,
        source_file: str = "",
        wing: str = "memory",
        room: str = "conversations",
        agent: str = "",
    ) -> List[str]:
        self._ensure_imported()
        src = source_file or "conversation_turn.md"
        if self._chunk_text_fn is None:
            did = self.add_drawer(content, wing=wing, room=room, source_file=src, agent=agent)
            return [did] if did else []

        added: List[str] = []
        try:
            for chunk in self._chunk_text_fn(content, src):
                body = chunk.get("content", "")
                if not body:
                    continue
                did = self.add_drawer(body, wing=wing, room=room, source_file=src, agent=agent)
                if did:
                    added.append(did)
        except Exception as e:
            logger.debug("[MemPalaceAPI] chunk_and_add failed: %s", e)
        return added

    def kg_add_triple(
        self,
        subject: str,
        predicate: str,
        obj: str,
        confidence: float = 1.0,
        valid_from: str = "",
        valid_to: str = "",
        source_closet: str = "",
        source_file: str = "",
        source_drawer_id: str = "",
    ) -> bool:
        kg = self._resolve_kg()
        if kg is None:
            return False
        try:
            kg.add_triple(
                subject,
                predicate,
                obj,
                valid_from=valid_from or None,
                valid_to=valid_to or None,
                confidence=confidence,
                source_closet=source_closet or None,
                source_file=source_file or None,
                source_drawer_id=source_drawer_id or None,
            )
            return True
        except Exception as e:
            logger.debug("[MemPalaceAPI] kg_add_triple failed: %s", e)
            return False

    def kg_invalidate_triple(self, subject: str, predicate: str, obj: str, ended: Optional[str] = None) -> bool:
        kg = self._resolve_kg()
        if kg is None:
            return False
        try:
            kg.invalidate(subject, predicate, obj, ended=ended or date.today().isoformat())
            return True
        except Exception as e:
            logger.debug("[MemPalaceAPI] kg_invalidate_triple failed: %s", e)
            return False

    def kg_query_entity(self, entity: str, direction: str = "both", as_of: Optional[str] = None) -> List[Dict[str, Any]]:
        kg = self._resolve_kg()
        if kg is None:
            return []
        try:
            if direction == "both":
                try:
                    outgoing = kg.query_entity(entity, as_of=as_of, direction="outgoing") or []
                    incoming = kg.query_entity(entity, as_of=as_of, direction="incoming") or []
                    return outgoing + incoming
                except TypeError:
                    return kg.query_entity(entity, direction="both") or []
            try:
                return kg.query_entity(entity, as_of=as_of, direction=direction) or []
            except TypeError:
                return kg.query_entity(entity, direction=direction) or []
        except Exception as e:
            logger.debug("[MemPalaceAPI] kg_query_entity failed: %s", e)
            return []

    def diary_write(self, agent_name: str, entry: str, topic: str = "general", wing: str = "") -> Dict[str, Any]:
        try:
            agent_name = self._sanitize_name(agent_name, "agent_name").lower()
            entry = self._sanitize_content(entry)
            topic = self._sanitize_name(topic, "topic")
            wing = self._sanitize_name(wing, "wing") if wing else f"wing_{agent_name.replace(' ', '_')}"
        except ValueError as e:
            return {"success": False, "error": str(e)}

        room = "diary"
        col = self._collection(create=True)
        if col is None:
            return {"success": False, "error": "No palace found"}

        now = datetime.now()
        entry_id = f"diary_{wing}_{now.strftime('%Y%m%d_%H%M%S%f')}_{hashlib.sha256(entry.encode()).hexdigest()[:12]}"
        base_metadata = {
            "wing": wing,
            "room": room,
            "hall": "hall_diary",
            "topic": topic,
            "type": "diary_entry",
            "agent": agent_name,
            "filed_at": now.isoformat(),
            "date": now.strftime("%Y-%m-%d"),
        }
        chunk_size = int(getattr(self._config, "chunk_size", 1200) or 1200)
        try:
            if len(entry) <= chunk_size:
                self._upsert(col, ids=[entry_id], documents=[entry], metadatas=[{**base_metadata, "chunk_index": 0}])
                return {
                    "success": True,
                    "entry_id": entry_id,
                    "agent": agent_name,
                    "topic": topic,
                    "timestamp": now.isoformat(),
                    "chunks": 1,
                }
            chunk_ids: List[str] = []
            chunk_docs: List[str] = []
            chunk_metas: List[Dict[str, Any]] = []
            for i in range(0, len(entry), chunk_size):
                chunk_idx = i // chunk_size
                chunk_ids.append(f"{entry_id}_chunk_{chunk_idx:06d}")
                chunk_docs.append(entry[i : i + chunk_size])
                chunk_metas.append({**base_metadata, "chunk_index": chunk_idx, "parent_entry_id": entry_id})
            self._upsert(col, ids=chunk_ids, documents=chunk_docs, metadatas=chunk_metas)
            return {
                "success": True,
                "entry_id": entry_id,
                "agent": agent_name,
                "topic": topic,
                "timestamp": now.isoformat(),
                "chunks": len(chunk_ids),
                "chunk_ids": chunk_ids,
            }
        except Exception as e:
            logger.debug("[MemPalaceAPI] diary_write failed: %s", e)
            return {"success": False, "error": str(e)}

    def diary_read(self, agent_name: str, last_n: int = 10, wing: str = "") -> Dict[str, Any]:
        try:
            agent_name = self._sanitize_name(agent_name, "agent_name").lower()
            wing = self._sanitize_name(wing, "wing") if wing else ""
        except ValueError as e:
            return {"error": str(e)}

        last_n = max(1, min(int(last_n), 100))
        col = self._collection()
        if col is None:
            return {"error": "No palace found"}

        conditions = [{"room": "diary"}, {"agent": agent_name}]
        if wing:
            conditions.insert(0, {"wing": wing})

        try:
            results = col.get(where={"$and": conditions}, include=["documents", "metadatas"], limit=10000)
            ids = self._result_field(results, "ids") or []
            if not ids:
                return {"agent": agent_name, "entries": [], "message": "No diary entries yet."}
            documents = self._result_field(results, "documents") or []
            metadatas = self._result_field(results, "metadatas") or []
            entries = []
            for doc, meta in zip(documents, metadatas):
                safe = self._safe_meta(meta)
                entries.append(
                    {
                        "date": safe.get("date", ""),
                        "timestamp": safe.get("filed_at", ""),
                        "topic": safe.get("topic", ""),
                        "content": doc,
                    }
                )
            entries.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
            entries = entries[:last_n]
            return {"agent": agent_name, "entries": entries, "total": len(ids), "showing": len(entries)}
        except Exception as e:
            logger.debug("[MemPalaceAPI] diary_read failed: %s", e)
            return {"error": str(e)}

    def graph_traverse(self, start_room: str, max_hops: int = 2, limit: int = 10) -> List[Dict[str, Any]]:
        self._ensure_imported()
        if self._traverse_fn is None:
            return []
        try:
            col = self._collection()
            results = self._traverse_fn(start_room, col=col, max_hops=max(1, min(int(max_hops), 10)))
            return results[:limit] if isinstance(results, list) else []
        except Exception as e:
            logger.debug("[MemPalaceAPI] graph_traverse failed: %s", e)
            return []

    def graph_find_tunnels(self, wing_a: Optional[str] = None, wing_b: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
        self._ensure_imported()
        if self._find_tunnels_fn is None:
            return []
        try:
            col = self._collection()
            results = self._find_tunnels_fn(wing_a=wing_a, wing_b=wing_b, col=col)
            return results[:limit] if isinstance(results, list) else []
        except Exception as e:
            logger.debug("[MemPalaceAPI] graph_find_tunnels failed: %s", e)
            return []

    def dialect_compress(self, text: str, metadata: Optional[Dict[str, Any]] = None) -> str:
        self._ensure_imported()
        try:
            from mempalace.dialect import Dialect
            dialect = Dialect()
            return dialect.compress(text, metadata=metadata) or ""
        except Exception as e:
            logger.debug("[MemPalaceAPI] dialect_compress failed: %s", e)
            return ""

    def strip_noise(self, text: str) -> str:
        """Remove system tags, hook output, and UI chrome from text.

        Uses mempalace.normalize.strip_noise — line-anchored patterns that
        preserve user prose mentioning these strings inline.
        Returns text unchanged if the import fails.
        """
        self._ensure_imported()
        try:
            from mempalace.normalize import strip_noise as _strip
            return _strip(text)
        except Exception as e:
            logger.debug("[MemPalaceAPI] strip_noise import failed: %s", e)
            return text

    def wake_up_context(self, wing: str = "", char_budget: int = 3200) -> str:
        self._ensure_imported()
        try:
            from mempalace.layers import MemoryStack
            stack = MemoryStack(self._palace_data_dir)
            return (stack.wake_up(wing=wing) or "")[:char_budget]
        except Exception as e:
            logger.debug("[MemPalaceAPI] wake_up_context failed: %s", e)
            return ""

    def scoped_recall(self, wing: str, room: Optional[str] = None, char_budget: int = 1500) -> str:
        self._ensure_imported()
        try:
            from mempalace.layers import MemoryStack
            stack = MemoryStack(self._palace_data_dir)
            return (stack.recall(wing=wing, room=room or "") or "")[:char_budget]
        except Exception as e:
            logger.debug("[MemPalaceAPI] scoped_recall failed: %s", e)
            return ""

    # ----------------------------------------------------------------
    # Dynamics (Hebb + Ebbinghaus + Cepeda) — living connection layer
    # ----------------------------------------------------------------

    def dynamics_apply(
        self,
        wing: Optional[str] = None,
        now: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Apply Ebbinghaus exponential decay to all halls and tunnels.

        Loads each record, calls ``apply_decay()`` (mutating in place),
        persists back to disk, and returns aggregate stats. Optional
        ``wing`` filter only touches halls in that wing and tunnels whose
        source or target is that wing.

        Pure admin operation. Call periodically (e.g. daily cron) or
        before a large prefetch batch.

        Returns a dict with: count, mean_strength_before, mean_strength_after,
        halls_touched, tunnels_touched, now (the timestamp used).
        """
        self._ensure_imported()
        if self._apply_decay_fn is None:
            return {"error": "mempalace.dynamics not importable", "count": 0}
        # Parse optional 'now' once
        parsed_now = None
        if now:
            try:
                from datetime import datetime, timezone
                v = now.strip()
                if v.endswith("Z"):
                    v = v[:-1] + "+00:00"
                parsed_now = datetime.fromisoformat(v)
                if parsed_now.tzinfo is None:
                    parsed_now = parsed_now.replace(tzinfo=timezone.utc)
            except Exception as e:
                return {"error": f"invalid 'now' timestamp: {e}"}

        stats = {
            "count": 0,
            "mean_strength_before": 0.0,
            "mean_strength_after": 0.0,
            "halls_touched": 0,
            "tunnels_touched": 0,
            "now": (parsed_now.isoformat() if parsed_now else None),
        }
        strengths_before: List[float] = []
        strengths_after: List[float] = []

        # --- Halls ---
        halls_dirty: List[Dict[str, Any]] = []
        if self._load_halls_fn is not None:
            try:
                all_halls = list(self._load_halls_fn() or [])
            except Exception as e:
                logger.debug("[MemPalaceAPI] load_halls failed: %s", e)
                all_halls = []
            for h in all_halls:
                if wing and h.get("wing") != wing:
                    continue
                strengths_before.append(float(h.get("strength", 1.0)))
                self._apply_decay_fn(h, now=parsed_now) if parsed_now else self._apply_decay_fn(h)
                strengths_after.append(float(h.get("strength", 1.0)))
                stats["count"] += 1
                stats["halls_touched"] += 1
                halls_dirty.append(h)
        # Persist halls (replace the touched wing's records; preserve others)
        if self._save_halls_fn is not None and halls_dirty:
            try:
                if wing:
                    other = [h for h in (self._load_halls_fn() or []) if h.get("wing") != wing]
                    self._save_halls_fn(other + halls_dirty)
                else:
                    self._save_halls_fn(halls_dirty)
            except Exception as e:
                logger.debug("[MemPalaceAPI] save_halls failed: %s", e)

        # --- Tunnels ---
        tunnels_dirty: List[Dict[str, Any]] = []
        if self._load_tunnels_fn is not None:
            try:
                all_tunnels = list(self._load_tunnels_fn() or [])
            except Exception as e:
                logger.debug("[MemPalaceAPI] load_tunnels failed: %s", e)
                all_tunnels = []
            for t in all_tunnels:
                # Tunnels are symmetric: source/target both count as "wing match"
                if wing:
                    sw = (t.get("source") or {}).get("wing") if isinstance(t.get("source"), dict) else t.get("source_wing")
                    tw = (t.get("target") or {}).get("wing") if isinstance(t.get("target"), dict) else t.get("target_wing")
                    if wing not in (sw, tw):
                        continue
                strengths_before.append(float(t.get("strength", 1.0)))
                self._apply_decay_fn(t, now=parsed_now) if parsed_now else self._apply_decay_fn(t)
                strengths_after.append(float(t.get("strength", 1.0)))
                stats["count"] += 1
                stats["tunnels_touched"] += 1
                tunnels_dirty.append(t)
        if self._save_tunnels_fn is not None and tunnels_dirty:
            try:
                if wing:
                    sw = wing
                    def _touches(t: Dict[str, Any]) -> bool:
                        s = (t.get("source") or {}).get("wing") if isinstance(t.get("source"), dict) else t.get("source_wing")
                        tg = (t.get("target") or {}).get("wing") if isinstance(t.get("target"), dict) else t.get("target_wing")
                        return sw in (s, tg)
                    other = [t for t in (self._load_tunnels_fn() or []) if not _touches(t)]
                    self._save_tunnels_fn(other + tunnels_dirty)
                else:
                    self._save_tunnels_fn(tunnels_dirty)
            except Exception as e:
                logger.debug("[MemPalaceAPI] save_tunnels failed: %s", e)

        if strengths_before:
            stats["mean_strength_before"] = round(sum(strengths_before) / len(strengths_before), 4)
        if strengths_after:
            stats["mean_strength_after"] = round(sum(strengths_after) / len(strengths_after), 4)
        return stats

    def potentiate(
        self,
        connection_id: str,
        kind: str = "tunnel",
        increment: float = 0.05,
    ) -> Dict[str, Any]:
        """Hebbian potentiation — strengthen a single hall or tunnel.

        The connection is found by ID in the appropriate store, updated
        in-memory via ``potentiate()``, and persisted back to disk. The
        full updated record is returned.
        """
        self._ensure_imported()
        if self._potentiate_fn is None:
            return {"error": "mempalace.dynamics not importable"}
        kind = (kind or "tunnel").lower()
        if kind not in ("hall", "tunnel"):
            return {"error": f"kind must be 'hall' or 'tunnel', got {kind!r}"}

        load_fn = self._load_halls_fn if kind == "hall" else self._load_tunnels_fn
        save_fn = self._save_halls_fn if kind == "hall" else self._save_tunnels_fn
        if load_fn is None or save_fn is None:
            return {"error": f"persistence helpers missing for {kind}"}
        try:
            records = list(load_fn() or [])
        except Exception as e:
            return {"error": f"load failed: {e}"}
        target: Optional[Dict[str, Any]] = None
        target_idx = -1
        for i, r in enumerate(records):
            if r.get("id") == connection_id:
                target = r
                target_idx = i
                break
        if target is None or target_idx < 0:
            return {"error": f"{kind} not found: {connection_id}"}
        try:
            self._potentiate_fn(target, increment=float(increment))
        except Exception as e:
            return {"error": f"potentiate failed: {e}"}
        records[target_idx] = target
        try:
            save_fn(records)
        except Exception as e:
            return {"error": f"persist failed: {e}", "record": target}
        return {"success": True, "kind": kind, "id": connection_id, "record": target}

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return deepcopy(self.TOOL_SPECS)

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        dispatch = {
            "mempalace_status": lambda a: self.tool_status(),
            "mempalace_list_wings": lambda a: self.tool_list_wings(),
            "mempalace_list_rooms": lambda a: self.tool_list_rooms(a.get("wing")),
            "mempalace_get_taxonomy": lambda a: self.tool_get_taxonomy(),
            "mempalace_get_aaak_spec": lambda a: self.tool_get_aaak_spec(),
            "mempalace_kg_query": lambda a: self.tool_kg_query(a["entity"], a.get("as_of"), a.get("direction", "both")),
            "mempalace_kg_add": lambda a: self.tool_kg_add(
                a["subject"], a["predicate"], a["object"], a.get("valid_from"), a.get("valid_to"), a.get("source_closet"), a.get("source_file"), a.get("source_drawer_id")
            ),
            "mempalace_kg_invalidate": lambda a: self.tool_kg_invalidate(a["subject"], a["predicate"], a["object"], a.get("ended")),
            "mempalace_kg_timeline": lambda a: self.tool_kg_timeline(a.get("entity")),
            "mempalace_kg_stats": lambda a: self.tool_kg_stats(),
            "mempalace_traverse": lambda a: self.tool_traverse(a["start_room"], a.get("max_hops", 2)),
            "mempalace_find_tunnels": lambda a: self.tool_find_tunnels(a.get("wing_a"), a.get("wing_b")),
            "mempalace_graph_stats": lambda a: self.tool_graph_stats(),
            "mempalace_create_tunnel": lambda a: self.tool_create_tunnel(
                a["source_wing"], a["source_room"], a["target_wing"], a["target_room"], a.get("label", ""), a.get("source_drawer_id"), a.get("target_drawer_id")
            ),
            "mempalace_list_tunnels": lambda a: self.tool_list_tunnels(a.get("wing")),
            "mempalace_delete_tunnel": lambda a: self.tool_delete_tunnel(a["tunnel_id"]),
            "mempalace_follow_tunnels": lambda a: self.tool_follow_tunnels(a["wing"], a["room"]),
            "mempalace_search": lambda a: self.tool_search(a["query"], a.get("limit", 5), a.get("wing"), a.get("room"), a.get("max_distance", 1.5), a.get("context")),
            "mempalace_check_duplicate": lambda a: self.tool_check_duplicate(a["content"], a.get("threshold", 0.9)),
            "mempalace_add_drawer": lambda a: self.tool_add_drawer(a["wing"], a["room"], a["content"], a.get("source_file"), a.get("added_by", "hermes")),
            "mempalace_delete_drawer": lambda a: self.tool_delete_drawer(a["drawer_id"]),
            "mempalace_sync": lambda a: self.tool_sync(a.get("project_dir"), a.get("wing"), bool(a.get("apply", False))),
            "mempalace_get_drawer": lambda a: self.tool_get_drawer(a["drawer_id"]),
            "mempalace_list_drawers": lambda a: self.tool_list_drawers(a.get("wing"), a.get("room"), a.get("limit", 20), a.get("offset", 0)),
            "mempalace_update_drawer": lambda a: self.tool_update_drawer(a["drawer_id"], a.get("content"), a.get("wing"), a.get("room")),
            "mempalace_diary_write": lambda a: self.diary_write(a["agent_name"], a["entry"], a.get("topic", "general"), a.get("wing", "")),
            "mempalace_diary_read": lambda a: self.diary_read(a["agent_name"], a.get("last_n", 10), a.get("wing", "")),
            "mempalace_hook_settings": lambda a: self.tool_hook_settings(a.get("silent_save"), a.get("desktop_toast")),
            "mempalace_memories_filed_away": lambda a: self.tool_memories_filed_away(),
            "mempalace_reconnect": lambda a: self.tool_reconnect(),
            "mempalace_dynamics_apply": lambda a: self.dynamics_apply(a.get("wing"), a.get("now")),
            "mempalace_potentiate": lambda a: self.potentiate(a["connection_id"], a.get("kind", "tunnel"), a.get("increment", 0.05)),
            "mempalace_repair_scan": lambda a: self.tool_repair_scan(a.get("wing")),
            "mempalace_repair_prune": lambda a: self.tool_repair_prune(bool(a.get("confirm", False))),
            "mempalace_export": lambda a: self.tool_export(a["output_dir"], a.get("format", "markdown")),
            "mempalace_dedup_stats": lambda a: self.tool_dedup_stats(a.get("wing")),
            "mempalace_dedup_run": lambda a: self.tool_dedup_run(a.get("wing"), a.get("threshold", 0.95), bool(a.get("dry_run", True))),
            "mempalace_detect_entities": lambda a: self.tool_detect_entities(a["text"], a.get("languages")),
            "mempalace_list_hallways": lambda a: self.tool_list_hallways(a.get("wing")),
            "mempalace_delete_hallway": lambda a: self.tool_delete_hallway(a["hallway_id"]),
        }
        handler = dispatch.get(tool_name)
        if handler is None:
            return {"error": f"Unknown MemPalace tool: {tool_name}"}
        try:
            return handler(args or {})
        except KeyError as e:
            return {"error": f"Missing required argument: {e.args[0]}"}
        except Exception as e:
            logger.exception("[MemPalaceAPI] tool dispatch failed for %s", tool_name)
            return {"error": str(e)}

    def tool_status(self) -> Dict[str, Any]:
        col = self._collection(create=Path(self._palace_data_dir, "chroma.sqlite3").exists())
        if col is None:
            return {"error": "No palace found", "hint": self._palace_data_dir}
        total = 0
        try:
            total = int(col.count())
        except Exception:
            pass
        wings: Dict[str, int] = {}
        rooms: Dict[str, int] = {}
        for meta in self._fetch_metadata():
            wing = meta.get("wing", "unknown")
            room = meta.get("room", "unknown")
            wings[wing] = wings.get(wing, 0) + 1
            rooms[room] = rooms.get(room, 0) + 1
        return {
            "total_drawers": total,
            "wings": wings,
            "rooms": rooms,
            "protocol": PALACE_PROTOCOL,
            "aaak_dialect": AAAK_SPEC,
        }

    def tool_list_wings(self) -> Dict[str, Any]:
        wings: Dict[str, int] = {}
        for meta in self._fetch_metadata():
            wing = meta.get("wing", "unknown")
            wings[wing] = wings.get(wing, 0) + 1
        return {"wings": wings}

    def tool_list_rooms(self, wing: Optional[str] = None) -> Dict[str, Any]:
        try:
            wing = self._sanitize_optional_name(wing, "wing")
        except ValueError as e:
            return {"error": str(e)}
        rooms: Dict[str, int] = {}
        for meta in self._fetch_metadata(where=self._build_where(wing=wing)):
            room = meta.get("room", "unknown")
            rooms[room] = rooms.get(room, 0) + 1
        return {"wing": wing or "all", "rooms": rooms}

    def tool_get_taxonomy(self) -> Dict[str, Any]:
        taxonomy: Dict[str, Dict[str, int]] = {}
        for meta in self._fetch_metadata():
            wing = meta.get("wing", "unknown")
            room = meta.get("room", "unknown")
            taxonomy.setdefault(wing, {})
            taxonomy[wing][room] = taxonomy[wing].get(room, 0) + 1
        return {"taxonomy": taxonomy}

    def tool_get_aaak_spec(self) -> Dict[str, Any]:
        return {"aaak_spec": AAAK_SPEC}

    def tool_kg_query(self, entity: str, as_of: Optional[str] = None, direction: str = "both") -> Dict[str, Any]:
        try:
            entity = self._sanitize_kg_value(entity, "entity")
            as_of = self._sanitize_iso_temporal(as_of, "as_of")
        except ValueError as e:
            return {"error": str(e)}
        if direction not in ("outgoing", "incoming", "both"):
            return {"error": "direction must be 'outgoing', 'incoming', or 'both'"}
        facts = self.kg_query_entity(entity, direction=direction, as_of=as_of)
        return {"entity": entity, "as_of": as_of, "facts": facts, "count": len(facts)}

    def tool_kg_add(
        self,
        subject: str,
        predicate: str,
        object: str,
        valid_from: Optional[str] = None,
        valid_to: Optional[str] = None,
        source_closet: Optional[str] = None,
        source_file: Optional[str] = None,
        source_drawer_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            subject = self._sanitize_kg_value(subject, "subject")
            predicate = self._sanitize_name(predicate, "predicate")
            object = self._sanitize_kg_value(object, "object")
            valid_from = self._sanitize_iso_temporal(valid_from, "valid_from")
            valid_to = self._sanitize_iso_temporal(valid_to, "valid_to")
        except ValueError as e:
            return {"success": False, "error": str(e)}
        ok = self.kg_add_triple(
            subject,
            predicate,
            object,
            valid_from=valid_from or "",
            valid_to=valid_to or "",
            source_closet=source_closet or "",
            source_file=source_file or "",
            source_drawer_id=source_drawer_id or "",
        )
        return {"success": ok, "fact": f"{subject} → {predicate} → {object}"} if ok else {"success": False, "error": "Failed to add fact"}

    def tool_kg_invalidate(self, subject: str, predicate: str, object: str, ended: Optional[str] = None) -> Dict[str, Any]:
        try:
            subject = self._sanitize_kg_value(subject, "subject")
            predicate = self._sanitize_name(predicate, "predicate")
            object = self._sanitize_kg_value(object, "object")
            ended = self._sanitize_iso_temporal(ended, "ended") or date.today().isoformat()
        except ValueError as e:
            return {"success": False, "error": str(e)}
        ok = self.kg_invalidate_triple(subject, predicate, object, ended=ended)
        return {"success": ok, "fact": f"{subject} → {predicate} → {object}", "ended": ended} if ok else {"success": False, "error": "Failed to invalidate fact"}

    def tool_kg_timeline(self, entity: Optional[str] = None) -> Dict[str, Any]:
        if entity is not None:
            try:
                entity = self._sanitize_kg_value(entity, "entity")
            except ValueError as e:
                return {"error": str(e)}
        kg = self._resolve_kg()
        if kg is None:
            return {"entity": entity or "all", "timeline": [], "count": 0}
        try:
            timeline = kg.timeline(entity)
            return {"entity": entity or "all", "timeline": timeline, "count": len(timeline)}
        except Exception as e:
            return {"error": str(e)}

    def tool_kg_stats(self) -> Dict[str, Any]:
        kg = self._resolve_kg()
        if kg is None:
            return {"entities": 0, "triples": 0}
        try:
            return kg.stats()
        except Exception as e:
            return {"error": str(e)}

    def tool_traverse(self, start_room: str, max_hops: int = 2) -> Dict[str, Any] | List[Dict[str, Any]]:
        try:
            start_room = self._sanitize_name(start_room, "start_room")
        except ValueError as e:
            return {"error": str(e)}
        return self.graph_traverse(start_room, max_hops=max_hops, limit=100)

    def tool_find_tunnels(self, wing_a: Optional[str] = None, wing_b: Optional[str] = None) -> Dict[str, Any] | List[Dict[str, Any]]:
        try:
            wing_a = self._sanitize_optional_name(wing_a, "wing_a")
            wing_b = self._sanitize_optional_name(wing_b, "wing_b")
        except ValueError as e:
            return {"error": str(e)}
        return self.graph_find_tunnels(wing_a=wing_a, wing_b=wing_b, limit=100)

    def tool_graph_stats(self) -> Dict[str, Any]:
        if self._graph_stats_fn is None:
            return {"error": "graph_stats unavailable"}
        try:
            col = self._collection()
            return self._graph_stats_fn(col=col)
        except Exception as e:
            return {"error": str(e)}

    def tool_create_tunnel(
        self,
        source_wing: str,
        source_room: str,
        target_wing: str,
        target_room: str,
        label: str = "",
        source_drawer_id: Optional[str] = None,
        target_drawer_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if self._create_tunnel_fn is None:
            return {"error": "create_tunnel unavailable"}
        try:
            source_wing = self._sanitize_name(source_wing, "source_wing")
            source_room = self._sanitize_name(source_room, "source_room")
            target_wing = self._sanitize_name(target_wing, "target_wing")
            target_room = self._sanitize_name(target_room, "target_room")
            return self._create_tunnel_fn(
                source_wing,
                source_room,
                target_wing,
                target_room,
                label=label,
                source_drawer_id=source_drawer_id,
                target_drawer_id=target_drawer_id,
            )
        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            return {"error": str(e)}

    def tool_list_tunnels(self, wing: Optional[str] = None) -> Dict[str, Any] | List[Dict[str, Any]]:
        if self._list_tunnels_fn is None:
            return {"error": "list_tunnels unavailable"}
        try:
            wing = self._sanitize_optional_name(wing, "wing")
            return self._list_tunnels_fn(wing)
        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            return {"error": str(e)}

    def tool_delete_tunnel(self, tunnel_id: str) -> Dict[str, Any]:
        if self._delete_tunnel_fn is None:
            return {"error": "delete_tunnel unavailable"}
        if not tunnel_id or not isinstance(tunnel_id, str):
            return {"error": "tunnel_id is required"}
        try:
            return self._delete_tunnel_fn(tunnel_id)
        except Exception as e:
            return {"error": str(e)}

    def tool_follow_tunnels(self, wing: str, room: str) -> Dict[str, Any] | List[Dict[str, Any]]:
        if self._follow_tunnels_fn is None:
            return {"error": "follow_tunnels unavailable"}
        try:
            wing = self._sanitize_name(wing, "wing")
            room = self._sanitize_name(room, "room")
            col = self._collection()
            return self._follow_tunnels_fn(wing, room, col=col)
        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            return {"error": str(e)}

    def tool_search(
        self,
        query: str,
        limit: int = 5,
        wing: Optional[str] = None,
        room: Optional[str] = None,
        max_distance: float = 1.5,
        context: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            wing = self._sanitize_optional_name(wing, "wing")
            room = self._sanitize_optional_name(room, "room")
        except ValueError as e:
            return {"error": str(e)}
        limit = max(1, min(int(limit), 100))
        dist = float(max_distance)
        min_score = 0.0 if dist == 0 else max(0.0, 1.0 - dist)
        results = self.search(query=query[:250], wing=wing or "", room=room or "", limit=limit, min_score=min_score)
        response: Dict[str, Any] = {"results": results, "count": len(results)}
        if context:
            response["context_received"] = True
        return response

    def tool_check_duplicate(self, content: str, threshold: float = 0.9) -> Dict[str, Any]:
        col = self._collection()
        if col is None:
            return {"error": "No palace found"}
        try:
            content = self._sanitize_content(content)
            results = col.query(query_texts=[content], n_results=5, include=["metadatas", "documents", "distances"])
            duplicates = []
            ids = self._result_field(results, "ids") or [[]]
            distances = self._result_field(results, "distances") or [[]]
            metadatas = self._result_field(results, "metadatas") or [[]]
            documents = self._result_field(results, "documents") or [[]]
            if ids and ids[0]:
                for i, drawer_id in enumerate(ids[0]):
                    distance = float(distances[0][i]) if distances and distances[0] and i < len(distances[0]) else 1.0
                    similarity = round(max(0.0, 1 - distance), 3)
                    if similarity >= float(threshold):
                        meta = self._safe_meta(metadatas[0][i] if metadatas and metadatas[0] and i < len(metadatas[0]) else {})
                        doc = (documents[0][i] if documents and documents[0] and i < len(documents[0]) else "") or ""
                        duplicates.append(
                            {
                                "id": drawer_id,
                                "wing": meta.get("wing", "?"),
                                "room": meta.get("room", "?"),
                                "similarity": similarity,
                                "content": doc[:200] + "..." if len(doc) > 200 else doc,
                            }
                        )
            return {"is_duplicate": bool(duplicates), "matches": duplicates}
        except Exception as e:
            logger.debug("[MemPalaceAPI] check_duplicate failed: %s", e)
            return {"error": "Duplicate check failed"}

    def tool_add_drawer(self, wing: str, room: str, content: str, source_file: Optional[str] = None, added_by: str = "hermes") -> Dict[str, Any]:
        try:
            wing = self._sanitize_name(wing, "wing")
            room = self._sanitize_name(room, "room")
            content = self._sanitize_content(content)
        except ValueError as e:
            return {"success": False, "error": str(e)}
        col = self._collection(create=True)
        if col is None:
            return {"success": False, "error": "No palace found"}

        drawer_id = self._fallback_drawer_id(wing, room, content)
        chunk_size = int(getattr(self._config, "chunk_size", 1200) or 1200)
        base_meta = {
            "wing": wing,
            "room": room,
            "source_file": source_file or "",
            "added_by": added_by,
            "filed_at": datetime.now().isoformat(),
        }

        if len(content) <= chunk_size:
            probe_ids = [drawer_id]
        else:
            last_chunk_idx = (len(content) - 1) // chunk_size
            probe_ids = [drawer_id, f"{drawer_id}_chunk_{last_chunk_idx:06d}"]
        try:
            existing = col.get(ids=probe_ids)
            existing_ids = self._result_field(existing, "ids") or []
            if existing_ids:
                return {"success": True, "reason": "already_exists", "drawer_id": drawer_id}
        except Exception:
            logger.debug("[MemPalaceAPI] add_drawer idempotency pre-check failed", exc_info=True)

        try:
            if len(content) <= chunk_size:
                self._upsert(col, ids=[drawer_id], documents=[content], metadatas=[{**base_meta, "chunk_index": 0}])
                return {"success": True, "drawer_id": drawer_id, "wing": wing, "room": room, "chunks": 1}
            chunk_ids: List[str] = []
            chunk_docs: List[str] = []
            chunk_metas: List[Dict[str, Any]] = []
            for i in range(0, len(content), chunk_size):
                chunk_idx = i // chunk_size
                chunk_ids.append(f"{drawer_id}_chunk_{chunk_idx:06d}")
                chunk_docs.append(content[i : i + chunk_size])
                chunk_metas.append({**base_meta, "chunk_index": chunk_idx, "parent_drawer_id": drawer_id})
            self._upsert(col, ids=chunk_ids, documents=chunk_docs, metadatas=chunk_metas)
            return {
                "success": True,
                "drawer_id": drawer_id,
                "wing": wing,
                "room": room,
                "chunks": len(chunk_ids),
                "chunk_ids": chunk_ids,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def tool_delete_drawer(self, drawer_id: str) -> Dict[str, Any]:
        col = self._collection()
        if col is None:
            return {"success": False, "error": "No palace found"}
        try:
            existing = col.get(ids=[drawer_id])
            ids = self._result_field(existing, "ids") or []
            if not ids:
                return {"success": False, "error": f"Drawer not found: {drawer_id}"}
            col.delete(ids=[drawer_id])
            return {"success": True, "drawer_id": drawer_id}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def tool_sync(self, project_dir: Optional[str] = None, wing: Optional[str] = None, apply: bool = False) -> Dict[str, Any]:
        self._ensure_imported()
        if self._sync_palace_fn is None:
            return {"success": False, "error": "sync unavailable"}
        try:
            report = self._sync_palace_fn(
                palace_path=self._palace_data_dir,
                project_dirs=[project_dir] if project_dir else None,
                wing=wing,
                dry_run=not apply,
            )
            payload = dict(vars(report)) if hasattr(report, "__dict__") else dict(report)
            payload["success"] = True
            if apply:
                self._col = None
            return payload
        except ValueError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": f"sync failed: {e}"}

    def tool_get_drawer(self, drawer_id: str) -> Dict[str, Any]:
        col = self._collection()
        if col is None:
            return {"error": "No palace found"}
        try:
            result = col.get(ids=[drawer_id], include=["documents", "metadatas"])
            ids = self._result_field(result, "ids") or []
            if not ids:
                return {"error": f"Drawer not found: {drawer_id}"}
            metadatas = self._result_field(result, "metadatas") or [{}]
            documents = self._result_field(result, "documents") or [""]
            meta = self._safe_meta(metadatas[0] if metadatas else {})
            safe_meta = dict(meta)
            if safe_meta.get("source_file"):
                safe_meta["source_file"] = Path(str(safe_meta["source_file"])).name
            return {
                "drawer_id": drawer_id,
                "content": documents[0] if documents else "",
                "wing": safe_meta.get("wing", ""),
                "room": safe_meta.get("room", ""),
                "metadata": safe_meta,
            }
        except Exception as e:
            return {"error": str(e)}

    def tool_list_drawers(self, wing: Optional[str] = None, room: Optional[str] = None, limit: int = 20, offset: int = 0) -> Dict[str, Any]:
        try:
            wing = self._sanitize_optional_name(wing, "wing")
            room = self._sanitize_optional_name(room, "room")
        except ValueError as e:
            return {"error": str(e)}
        col = self._collection()
        if col is None:
            return {"error": "No palace found"}
        limit = max(1, min(int(limit), 100))
        offset = max(0, int(offset))
        where = self._build_where(wing=wing, room=room)
        try:
            kwargs: Dict[str, Any] = {"include": ["documents", "metadatas"], "limit": limit, "offset": offset}
            if where:
                kwargs["where"] = where
            result = col.get(**kwargs)
            ids = self._result_field(result, "ids") or []
            metadatas = self._result_field(result, "metadatas") or []
            documents = self._result_field(result, "documents") or []

            total = self._count_rows(where)

            drawers = []
            for i, did in enumerate(ids):
                meta = self._safe_meta(metadatas[i] if i < len(metadatas) else {})
                doc = documents[i] if i < len(documents) else ""
                drawers.append(
                    {
                        "drawer_id": did,
                        "wing": meta.get("wing", ""),
                        "room": meta.get("room", ""),
                        "content_preview": doc[:200] + "..." if len(doc) > 200 else doc,
                    }
                )
            return {"drawers": drawers, "total": total, "count": len(drawers), "offset": offset, "limit": limit}
        except Exception as e:
            return {"error": str(e)}

    def tool_update_drawer(self, drawer_id: str, content: Optional[str] = None, wing: Optional[str] = None, room: Optional[str] = None) -> Dict[str, Any]:
        if content is None and wing is None and room is None:
            return {"success": True, "drawer_id": drawer_id, "noop": True}
        col = self._collection()
        if col is None:
            return {"success": False, "error": "No palace found"}
        try:
            existing = col.get(ids=[drawer_id], include=["documents", "metadatas"])
            ids = self._result_field(existing, "ids") or []
            if not ids:
                return {"success": False, "error": f"Drawer not found: {drawer_id}"}
            metadatas = self._result_field(existing, "metadatas") or [{}]
            documents = self._result_field(existing, "documents") or [""]
            old_meta = self._safe_meta(metadatas[0] if metadatas else {})
            old_doc = documents[0] if documents else ""
            new_doc = self._sanitize_content(content) if content is not None else old_doc
            new_meta = dict(old_meta)
            if wing is not None:
                new_meta["wing"] = self._sanitize_name(wing, "wing")
            if room is not None:
                new_meta["room"] = self._sanitize_name(room, "room")
            update_kwargs: Dict[str, Any] = {"ids": [drawer_id], "metadatas": [new_meta]}
            if content is not None:
                update_kwargs["documents"] = [new_doc]
            col.update(**update_kwargs)
            return {"success": True, "drawer_id": drawer_id, "wing": new_meta.get("wing", ""), "room": new_meta.get("room", "")}
        except ValueError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def tool_hook_settings(self, silent_save: Optional[bool] = None, desktop_toast: Optional[bool] = None) -> Dict[str, Any]:
        self._ensure_imported()
        if self._native_config_cls is None:
            return {"success": False, "error": "native MemPalace config unavailable"}
        try:
            native = self._native_config_cls()
            changed = []
            if silent_save is not None:
                native.set_hook_setting("silent_save", silent_save)
                changed.append(f"silent_save → {silent_save}")
            if desktop_toast is not None:
                native.set_hook_setting("desktop_toast", desktop_toast)
                changed.append(f"desktop_toast → {desktop_toast}")
            native = self._native_config_cls()
            result = {
                "success": True,
                "settings": {
                    "silent_save": native.hook_silent_save,
                    "desktop_toast": native.hook_desktop_toast,
                },
            }
            if changed:
                result["updated"] = changed
            return result
        except Exception as e:
            return {"success": False, "error": str(e)}

    def tool_memories_filed_away(self) -> Dict[str, Any]:
        state_dir = Path.home() / ".mempalace" / "hook_state"
        ack_file = state_dir / "last_checkpoint"
        if not ack_file.is_file():
            return {"status": "quiet", "message": "No recent journal entry", "count": 0, "timestamp": None}
        try:
            data = json.loads(ack_file.read_text(encoding="utf-8"))
            ack_file.unlink(missing_ok=True)
            msgs = data.get("msgs", 0)
            return {"status": "ok", "message": f"✦ {msgs} messages tucked into drawers", "count": msgs, "timestamp": data.get("ts")}
        except (json.JSONDecodeError, OSError):
            ack_file.unlink(missing_ok=True)
            return {"status": "error", "message": "✦ Journal entry filed in the palace", "count": 0, "timestamp": None}

    def tool_reconnect(self) -> Dict[str, Any]:
        close_errors: List[str] = []
        self._col = None
        self._kg = None
        try:
            if self._shared_system_client is not None:
                clear_system_cache = getattr(self._shared_system_client, "clear_system_cache", None)
                if callable(clear_system_cache):
                    clear_system_cache()
        except Exception as e:
            close_errors.append(f"shared Chroma cache clear failed: {e}")
        try:
            col = self._collection()
            if col is None:
                result = {"success": False, "message": "No palace found after reconnect", "drawers": 0}
                if close_errors:
                    result["error"] = "; ".join(close_errors)
                return result
            result = {"success": not bool(close_errors), "message": "Reconnected to palace", "drawers": int(col.count())}
            if close_errors:
                result["error"] = "; ".join(close_errors)
            return result
        except Exception as e:
            return {"success": False, "error": str(e)}

    def tool_repair_scan(self, wing: Optional[str] = None) -> Dict[str, Any]:
        self._ensure_imported()
        if self._repair_scan_fn is None:
            return {"error": "repair.scan_palace not available — mempalace.repair module could not be imported"}
        try:
            result = self._repair_scan_fn(palace_path=self._palace_data_dir, only_wing=wing)
            if isinstance(result, dict):
                return result
            return {"result": result}
        except Exception as e:
            return {"error": f"repair scan failed: {e}"}

    def tool_repair_prune(self, confirm: bool = False) -> Dict[str, Any]:
        self._ensure_imported()
        if self._repair_prune_fn is None:
            return {"error": "repair.prune_corrupt not available — mempalace.repair module could not be imported"}
        try:
            result = self._repair_prune_fn(palace_path=self._palace_data_dir, confirm=confirm)
            if isinstance(result, dict):
                return result
            return {"result": result}
        except Exception as e:
            return {"error": f"repair prune failed: {e}"}

    def tool_export(self, output_dir: str, format: str = "markdown") -> Dict[str, Any]:
        self._ensure_imported()
        if self._export_palace_fn is None:
            return {"error": "exporter.export_palace not available — mempalace.exporter module could not be imported"}
        try:
            result = self._export_palace_fn(palace_path=self._palace_data_dir, output_dir=output_dir, format=format)
            if isinstance(result, dict):
                return result
            return {"result": result, "output_dir": output_dir}
        except Exception as e:
            return {"error": f"export failed: {e}"}

    def tool_dedup_stats(self, wing: Optional[str] = None) -> Dict[str, Any]:
        self._ensure_imported()
        if self._dedup_stats_fn is None:
            return {"error": "dedup.show_stats not available — mempalace.dedup module could not be imported"}
        try:
            result = self._dedup_stats_fn(palace_path=self._palace_data_dir)
            if isinstance(result, dict):
                return result
            return {"result": result}
        except Exception as e:
            return {"error": f"dedup stats failed: {e}"}

    def tool_dedup_run(self, wing: Optional[str] = None, threshold: float = 0.95, dry_run: bool = True) -> Dict[str, Any]:
        self._ensure_imported()
        if self._dedup_run_fn is None:
            return {"error": "dedup.dedup_palace not available — mempalace.dedup module could not be imported"}
        try:
            result = self._dedup_run_fn(palace_path=self._palace_data_dir, threshold=threshold, dry_run=dry_run, wing=wing)
            if not dry_run:
                self._col = None
            if isinstance(result, dict):
                return result
            return {"result": result}
        except Exception as e:
            return {"error": f"dedup run failed: {e}"}

    def tool_detect_entities(self, text: str, languages: Optional[List[str]] = None) -> Dict[str, Any]:
        """Detect entities (people, projects, topics) from text.

        In MemPalace v3.5.0, library detect_entities() changed to take
        file_paths instead of text. This method preserves the text-based
        interface by calling extract_candidates + classify_entity directly,
        which is what detect_entities does internally after reading files.
        """
        self._ensure_imported()
        langs = tuple(languages) if languages else ("en",)

        # Fast path: text-based pipeline (always available in v3.5.0)
        if self._extract_candidates_fn is not None:
            try:
                candidates = self._extract_candidates_fn(text, languages=langs)
                if not candidates:
                    return {"people": [], "projects": [], "topics": [], "uncertain": []}

                # Classify each candidate
                people, projects, topics, uncertain = [], [], [], []
                for name, freq in candidates.items():
                    if self._classify_entity_fn:
                        try:
                            scores: dict = {"person_score": 0, "project_score": 0}
                            entity = self._classify_entity_fn(name, freq, scores)
                            etype = entity.get("type", "uncertain")
                            entity["frequency"] = freq
                            if etype == "person":
                                people.append(entity)
                            elif etype == "project":
                                projects.append(entity)
                            else:
                                uncertain.append(entity)
                        except Exception:
                            uncertain.append({"name": name, "type": "uncertain", "frequency": freq})
                    else:
                        uncertain.append({"name": name, "type": "uncertain", "frequency": freq})

                return {
                    "people": people,
                    "projects": projects,
                    "topics": topics,
                    "uncertain": uncertain,
                }
            except Exception as e:
                return {"error": f"text entity detection failed: {e}"}

        # Legacy fallback: old detect_entities(text, languages=...) API
        if self._detect_entities_fn is not None:
            try:
                import inspect as _inspect
                sig = _inspect.signature(self._detect_entities_fn)
                first_param = next(iter(sig.parameters.values()), None)
                if first_param and "file" in str(first_param).lower():
                    return {"error": "detect_entities now requires file paths in v3.5.0; text mode unavailable"}
                result = self._detect_entities_fn(text, languages=langs)
                if isinstance(result, dict):
                    return result
                return {"result": result}
            except Exception as e:
                return {"error": f"detect entities failed: {e}"}

        return {"error": "entity_detector not available — mempalace.entity_detector module could not be imported"}

    # ----------------------------------------------------------------
    # v3.5.0: Hallways management
    # ----------------------------------------------------------------

    def tool_list_hallways(self, wing: Optional[str] = None) -> Dict[str, Any]:
        """List within-wing hallway records (entity-to-entity co-occurrence connections)."""
        self._ensure_imported()
        if self._list_hallways_fn is None:
            return {"error": "hallways.list_hallways not available"}
        try:
            result = self._list_hallways_fn(wing=wing) if wing else self._list_hallways_fn()
            if isinstance(result, list):
                return {"hallways": result, "count": len(result)}
            return {"result": result}
        except Exception as e:
            return {"error": f"list hallways failed: {e}"}

    def tool_delete_hallway(self, hallway_id: str) -> Dict[str, Any]:
        """Delete a hallway record by its ID."""
        self._ensure_imported()
        if self._delete_hallway_fn is None:
            return {"error": "hallways.delete_hallway not available"}
        if not hallway_id or not isinstance(hallway_id, str):
            return {"error": "hallway_id is required"}
        try:
            result = self._delete_hallway_fn(hallway_id)
            return {"deleted": bool(result)}
        except Exception as e:
            return {"error": f"delete hallway failed: {e}"}

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "enabled", "type": "bool", "default": True, "description": "Enable the MemPalace memory provider"},
            {"key": "palace_data_dir", "type": "path", "default": "~/.mempalace/palace", "description": "ChromaDB data directory"},
            {"key": "mempalace_lib_dir", "type": "path", "default": "~/.openclaw/workspace/mempalace", "description": "MemPalace Python package checkout"},
            {"key": "ingestion.mode", "type": "str", "default": "none", "description": "each_turn | session_end | none"},
            {"key": "ingestion.min_turn_length", "type": "int", "default": 20},
            {"key": "ingestion.max_turn_length", "type": "int", "default": 8000},
            {"key": "retrieval.enabled", "type": "bool", "default": True},
            {"key": "retrieval.max_results", "type": "int", "default": 8},
            {"key": "retrieval.min_score", "type": "float", "default": 0.3},
            {"key": "retrieval.include_kg_facts", "type": "bool", "default": False},
            {"key": "retrieval.timeout_ms", "type": "int", "default": 500},
            {"key": "facts.extract_each_turn", "type": "bool", "default": False},
            {"key": "memory_mirror.enabled", "type": "bool", "default": False},
            {"key": "holographic.enabled", "type": "bool", "default": False},
            {"key": "graph.enabled", "type": "bool", "default": False},
            {"key": "diary.enabled", "type": "bool", "default": False},
            {"key": "aaak.enabled", "type": "bool", "default": False},
            {"key": "duplicate_check_enabled", "type": "bool", "default": True},
            {"key": "duplicate_threshold", "type": "float", "default": 0.9},
            {"key": "cache_ttl_seconds", "type": "int", "default": 30},
        ]
