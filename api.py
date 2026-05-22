"""MemPalace API — thin, fail-open wrapper around MemPalace operations.

All operations are fail-open: errors are logged and return safe empty/error
values rather than propagating. This keeps Hermes chat running even if
MemPalace has issues.
"""
from __future__ import annotations

import hashlib
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import MemPalaceConfig

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------
# Utility
# --------------------------------------------------------------------


def _normalize(s: str) -> str:
    """Normalize a string for lexical matching."""
    return " ".join(s.lower().replace("-", " ").replace("_", " ").split())


# --------------------------------------------------------------------
# MemPalaceAPI
# --------------------------------------------------------------------


class MemPalaceAPI:
    """Thin, fail-open MemPalace operations wrapper.

    Designed to be instantiated with a config object and used as
    ``provider._mp_api``. All errors are caught and logged; no operation
    raises an exception to the caller.
    """

    def __init__(
        self,
        palace_data_dir: str = "",
        mempalace_lib_dir: str = "",
        config: Optional[MemPalaceConfig] = None,
    ):
        self._palace_data_dir = palace_data_dir or ""
        self._mempalace_lib_dir = mempalace_lib_dir or ""
        self._config = config or MemPalaceConfig()

        self._imported = False
        self._import_error: Optional[str] = None

        # Lazy-imported functions (set in _ensure_imported)
        self._search_memories_fn: Any = None
        self._get_collection_fn: Any = None
        self._miner_add_drawer_fn: Any = None
        self._chunk_text_fn: Any = None

        # Palace handle (set on first use)
        self._palace: Any = None

        # Cached collection handle
        self._col: Any = None

        # KG handle
        self._kg: Any = None

        # Diagonal state for lazy init
        self._initialized = False

    # ----------------------------------------------------------------
    # Import / availability
    # ----------------------------------------------------------------

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

        # Granular: each import is independent — partial failures are fine
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

        self._imported = bool(self._search_memories_fn or self._get_collection_fn)
        if not self._imported:
            self._import_error = "No mempalace modules could be imported"
            logger.warning("[MemPalaceAPI] mempalace import failed — no modules available")
        else:
            self._import_error = None

    @property
    def is_available(self) -> bool:
        if not self._palace_data_dir or not Path(self._palace_data_dir).exists():
            return False
        self._ensure_imported()
        return bool(self._imported)

    # ----------------------------------------------------------------
    # Collection
    # ----------------------------------------------------------------

    def _collection(self, create: bool = False) -> Any:
        """Get (or create) the drawers collection, or None."""
        if self._col is not None:
            return self._col
        self._ensure_imported()
        if self._get_collection_fn is None:
            return None
        try:
            self._col = self._get_collection_fn(
                self._palace_data_dir, create=create
            )
        except Exception as e:
            logger.debug("[MemPalaceAPI] get_collection failed: %s", e)
            return None
        return self._col

    # ----------------------------------------------------------------
    # Duplicate check
    # ----------------------------------------------------------------

    def _check_duplicate(self, content: str, col: Any) -> Optional[str]:
        """Return existing drawer ID if near-duplicate found, else None."""
        if not self._config.duplicate_check_enabled:
            return None
        try:
            threshold = 1.0 - float(self._config.duplicate_threshold)
            result = col.query(
                query_texts=[content],
                n_results=1,
                include=["distances"],
            )
            distances = result.get("distances") or [[]]
            if distances and distances[0] and distances[0][0] <= threshold:
                existing_ids = result.get("ids") or [[]]
                if existing_ids and existing_ids[0]:
                    return str(existing_ids[0][0])
        except Exception as e:
            logger.debug("[MemPalaceAPI] duplicate check failed: %s", e)
        return None

    # ----------------------------------------------------------------
    # Search
    # ----------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        wing: str = "",
        room: str = "",
        limit: int = 8,
        min_score: float = 0.3,
    ) -> List[Dict[str, Any]]:
        """Hybrid semantic + lexical fallback search. Fail-open."""
        if not query:
            return []
        self._ensure_imported()

        # Semantic search
        semantic = self._semantic_search(query, wing=wing, room=room, limit=limit, min_score=min_score)

        # If fewer results than limit, try lexical fallback
        if len(semantic) < limit:
            lexical = self._lexical_fallback(query, limit=limit - len(semantic))
            seen_ids = {r["drawer_id"] for r in semantic}
            for r in lexical:
                if r["drawer_id"] not in seen_ids:
                    semantic.append(r)
                    seen_ids.add(r["drawer_id"])

        return semantic

    def _semantic_search(
        self, query: str, wing: str = "", room: str = "", limit: int = 8, min_score: float = 0.3
    ) -> List[Dict[str, Any]]:
        """Run mempalace search_memories with timeout guard."""
        search_fn = self._search_memories_fn
        if search_fn is None:
            return []
        max_dist = max(0.0, 1.0 - float(min_score)) if min_score > 0 else 0.0
        try:
            raw = search_fn(
                query,
                self._palace_data_dir,
                wing=wing or None,
                room=room or None,
                n_results=limit,
                max_distance=max_dist,
            )
        except TypeError:
            try:
                raw = search_fn(
                    query=query,
                    palace_path=self._palace_data_dir,
                    wing=wing or None,
                    room=room or None,
                    n_results=limit,
                    max_distance=max_dist,
                )
            except Exception as e:
                logger.debug("[MemPalaceAPI] search_memories kwargs failed: %s", e)
                raw = None
        except Exception as e:
            logger.debug("[MemPalaceAPI] search_memories failed: %s", e)
            raw = None

        if isinstance(raw, dict) and raw.get("error"):
            return []
        results = []
        for h in (raw or {}).get("results") or []:
            sim = float(h.get("similarity", 0.0))
            if sim < min_score:
                continue
            results.append({
                "content": h.get("text", ""),
                "score": sim,
                "wing": h.get("wing", "?"),
                "room": h.get("room", "?"),
                "source_file": h.get("source_file", "?"),
                "drawer_id": h.get("drawer_id", ""),
                "match_type": str(h.get("matched_via", "semantic")),
            })
        return results

    def _lexical_fallback(self, query: str, limit: int = 4) -> List[Dict[str, Any]]:
        """Lexical fallback over drawer IDs, source paths, wing/room, doc prefix.

        Chroma `.get(ids=[...])` must NOT include 'ids' in the 'include' param.
        """
        if not query:
            return []
        col = self._collection()
        if col is None:
            return []

        results = []
        norm = _normalize(query)

        # 1. Exact drawer ID match
        if len(results) < limit:
            try:
                ids_to_check = [query.strip()]
                # Don't pass 'ids' in 'include' — Chroma rejects that combination
                hit = col.get(ids=ids_to_check)
                if hit and hit.get("ids"):
                    results.append({
                        "content": (hit.get("documents") or [""])[0],
                        "score": 1.0,
                        "wing": (hit.get("metadatas") or [{}])[0].get("wing", "?"),
                        "room": (hit.get("metadatas") or [{}])[0].get("room", "?"),
                        "source_file": (hit.get("metadatas") or [{}])[0].get("source_file", "?"),
                        "drawer_id": hit["ids"][0],
                        "match_type": "lexical:id",
                    })
            except Exception as e:
                logger.debug("[MemPalaceAPI] lexical drawer-id lookup failed: %s", e)

        # 2. Source file variant matching (e.g. using-superpowers → using_superpowers)
        if len(results) < limit:
            parts = norm.replace(" ", "-").split("-")
            for i in range(len(parts)):
                variant = "_".join(parts[i:])
                if len(variant) < 3:
                    continue
                try:
                    hit = col.get(ids=[f"drawer_{variant}"])
                    if hit and hit.get("ids"):
                        results.append({
                            "content": (hit.get("documents") or [""])[0],
                            "score": 0.9,
                            "wing": (hit.get("metadatas") or [{}])[0].get("wing", "?"),
                            "room": (hit.get("metadatas") or [{}])[0].get("room", "?"),
                            "source_file": (hit.get("metadatas") or [{}])[0].get("source_file", "?"),
                            "drawer_id": hit["ids"][0],
                            "match_type": "lexical:variant",
                        })
                        break
                except Exception:
                    pass

        # 3. Where search over source_file, wing, room, doc prefix
        if len(results) < limit:
            try:
                scan_limit = getattr(self._config, "lexical_scan_limit", 1000)
                # Chroma where filter — scan all and filter in Python
                all_results = col.get(limit=scan_limit)
                ids = all_results.get("ids") or []
                docs = all_results.get("documents") or []
                metas = all_results.get("metadatas") or []
                for i, did in enumerate(ids):
                    if len(results) >= limit:
                        break
                    meta = metas[i] if i < len(metas) else {}
                    sf = meta.get("source_file", "")
                    w = meta.get("wing", "")
                    r = meta.get("room", "")
                    doc = docs[i] if i < len(docs) else ""

                    sf_norm = _normalize(sf)
                    doc_norm = _normalize(doc[:100])

                    if (norm in sf_norm or norm in doc_norm or
                            _normalize(w) in norm or _normalize(r) in norm):
                        results.append({
                            "content": doc[:300],
                            "score": 0.7,
                            "wing": w,
                            "room": r,
                            "source_file": sf,
                            "drawer_id": did if isinstance(did, str) else str(i),
                            "match_type": "lexical:meta",
                        })
            except Exception as e:
                logger.debug("[MemPalaceAPI] lexical meta scan failed: %s", e)

        return results

    # ----------------------------------------------------------------
    # Write
    # ----------------------------------------------------------------

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
        """Add a verbatim drawer with optional duplicate check.

        Returns a drawer ID string. Fails open — returns a deterministic
        ID on error rather than raising.
        """
        col = self._collection(create=True)
        if col is None:
            return self._fallback_drawer_id(wing, room, content)

        existing = self._check_duplicate(content, col)
        if existing:
            return existing

        return self._write_drawer(col, content, wing, room, source_file, agent)

    def _write_drawer(
        self, col: Any, content: str, wing: str, room: str, source_file: str, agent: str
    ) -> str:
        drawer_id = f"drawer_{wing}_{room}_{hashlib.sha256((wing + room + content).encode()).hexdigest()[:24]}"
        src = source_file or "inline.md"
        try:
            if self._miner_add_drawer_fn:
                result = self._miner_add_drawer_fn(col, wing, room, content, src, 0, agent or "hermes")
                # Prefer returned drawer_id from the miner
                if isinstance(result, dict) and result.get("drawer_id"):
                    drawer_id = str(result["drawer_id"])
            else:
                col.add(
                    ids=[drawer_id],
                    documents=[content],
                    metadatas=[{
                        "wing": wing, "room": room, "source_file": src,
                        "agent": agent or "hermes", "chunk_index": 0,
                    }],
                )
        except Exception as e:
            logger.debug("[MemPalaceAPI] _write_drawer failed: %s", e)
        return drawer_id

    def _fallback_drawer_id(self, wing: str, room: str, content: str) -> str:
        return f"drawer_{wing}_{room}_{hashlib.sha256((wing + room + content).encode()).hexdigest()[:24]}"

    def chunk_and_add(
        self,
        content: str,
        *,
        source_file: str = "",
        wing: str = "memory",
        room: str = "conversations",
        agent: str = "",
    ) -> List[str]:
        """Chunk content and add each chunk as a drawer. All chunks use duplicate checks."""
        self._ensure_imported()
        col = self._collection(create=True)
        src = source_file or "conversation_turn.md"

        # Without chunker, write directly with duplicate check
        if self._chunk_text_fn is None:
            did = self.add_drawer(content, wing=wing, room=room, source_file=src, agent=agent)
            return [did] if did else []

        added: List[str] = []
        try:
            for chunk in self._chunk_text_fn(content, src):
                body = chunk.get("content", "")
                idx = int(chunk.get("chunk_index", 0))
                if not body:
                    continue
                did = self.add_drawer(
                    body,
                    wing=wing,
                    room=room,
                    source_file=src,
                    agent=agent,
                )
                if did:
                    added.append(did)
        except Exception as e:
            logger.debug("[MemPalaceAPI] chunk_and_add failed: %s", e)

        return added

    # ----------------------------------------------------------------
    # Knowledge graph
    # ----------------------------------------------------------------

    def _resolve_kg(self) -> Any:
        if self._kg is not None:
            return self._kg
        self._ensure_imported()
        if not self._imported:
            return None
        try:
            from mempalace.knowledge_graph import KnowledgeGraph as _KG

            db_path = None
            if self._palace_data_dir:
                db_path = str(Path(self._palace_data_dir).parent / "knowledge_graph.sqlite3")
            self._kg = _KG(db_path=db_path)
        except Exception as e:
            logger.warning("[MemPalaceAPI] KnowledgeGraph unavailable: %s", e)
            self._kg = None
        return self._kg

    def kg_add_triple(
        self, subject: str, predicate: str, obj: str, confidence: float = 0.9, valid_from: str = ""
    ) -> bool:
        """Add a KG triple. Fail-open."""
        kg = self._resolve_kg()
        if kg is None:
            return False
        try:
            kg.add(subject, predicate, obj, confidence=confidence, valid_from=valid_from)
            return True
        except Exception as e:
            logger.debug("[MemPalaceAPI] kg_add_triple failed: %s", e)
            return False

    def kg_invalidate_triple(
        self, subject: str, predicate: str, obj: str, ended: Optional[str] = None
    ) -> bool:
        """Invalidate a KG triple. Fail-open."""
        kg = self._resolve_kg()
        if kg is None:
            return False
        try:
            kg.invalidate(subject, predicate, obj, ended=ended or "")
            return True
        except Exception as e:
            logger.debug("[MemPalaceAPI] kg_invalidate_triple failed: %s", e)
            return False

    def kg_query_entity(self, entity: str, direction: str = "both") -> List[Dict[str, Any]]:
        """Query all relationships for an entity. Fail-open."""
        kg = self._resolve_kg()
        if kg is None:
            return []
        try:
            return kg.query_entity(entity, direction=direction)
        except Exception as e:
            logger.debug("[MemPalaceAPI] kg_query_entity failed: %s", e)
            return []

    # ----------------------------------------------------------------
    # Diary
    # ----------------------------------------------------------------

    def diary_write(
        self, agent_name: str, entry: str, topic: str = "general", wing: str = ""
    ) -> Dict[str, Any]:
        """Write a diary entry. Fail-open."""
        self._ensure_imported()
        try:
            from mempalace.mcp_server import tool_diary_write as _diary_write
            return _diary_write(agent_name, entry, topic=topic, wing=wing)
        except Exception as e:
            logger.debug("[MemPalaceAPI] diary_write failed: %s", e)
            return {"success": False, "error": str(e)}

    def diary_read(
        self, agent_name: str, last_n: int = 10, wing: str = ""
    ) -> Dict[str, Any]:
        """Read recent diary entries. Fail-open."""
        self._ensure_imported()
        try:
            from mempalace.mcp_server import tool_diary_read as _diary_read
            return _diary_read(agent_name, last_n=last_n, wing=wing)
        except Exception as e:
            logger.debug("[MemPalaceAPI] diary_read failed: %s", e)
            return {"error": str(e)}

    # ----------------------------------------------------------------
    # Graph
    # ----------------------------------------------------------------

    def graph_traverse(
        self, start_room: str, max_hops: int = 2, limit: int = 10
    ) -> List[Dict[str, Any]]:
        """BFS walk from a room. Fail-open."""
        self._ensure_imported()
        try:
            from mempalace.palace_graph import traverse as _traverse
            results = _traverse(start_room, max_hops=max_hops)
            return results[:limit] if isinstance(results, list) else []
        except Exception as e:
            logger.debug("[MemPalaceAPI] graph_traverse failed: %s", e)
            return []

    def graph_find_tunnels(
        self, wing_a: Optional[str] = None, wing_b: Optional[str] = None, limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Find rooms that bridge wings. Fail-open."""
        self._ensure_imported()
        try:
            from mempalace.palace_graph import find_tunnels as _find_tunnels
            results = _find_tunnels(wing_a=wing_a, wing_b=wing_b)
            return results[:limit] if isinstance(results, list) else []
        except Exception as e:
            logger.debug("[MemPalaceAPI] graph_find_tunnels failed: %s", e)
            return []

    # ----------------------------------------------------------------
    # AAAK dialect
    # ----------------------------------------------------------------

    def dialect_compress(self, text: str, metadata: Optional[Dict[str, Any]] = None) -> str:
        """Compress text into AAAK dialect format. Fail-open."""
        self._ensure_imported()
        try:
            from mempalace.dialect import Dialect
            config_path = getattr(self, "_aaak_config_path", "") or os.environ.get("MEMPALACE_AAAK_CONFIG", "")
            dialect = Dialect.from_config(config_path) if config_path and Path(config_path).exists() else Dialect()
            return dialect.compress(text, metadata=metadata) or ""
        except Exception as e:
            logger.debug("[MemPalaceAPI] dialect_compress failed: %s", e)
            return ""

    # ----------------------------------------------------------------
    # Memory stack (L0-L3)
    # ----------------------------------------------------------------

    def wake_up_context(self, wing: str = "", char_budget: int = 3200) -> str:
        """Get L0+L1 wake-up context from MemoryStack. Fail-open."""
        self._ensure_imported()
        try:
            from mempalace.layers import MemoryStack
            stack = MemoryStack(self._palace_data_dir)
            return stack.wake_up(wing=wing)[:char_budget]
        except Exception as e:
            logger.debug("[MemPalaceAPI] wake_up_context failed: %s", e)
            return ""

    def scoped_recall(
        self, wing: str, room: Optional[str] = None, char_budget: int = 1500
    ) -> str:
        """L2 scoped recall. Fail-open."""
        self._ensure_imported()
        try:
            from mempalace.layers import MemoryStack
            stack = MemoryStack(self._palace_data_dir)
            return stack.recall(wing=wing, room=room or "")[:char_budget]
        except Exception as e:
            logger.debug("[MemPalaceAPI] scoped_recall failed: %s", e)
            return ""

    # ----------------------------------------------------------------
    # Config schema
    # ----------------------------------------------------------------

    def get_config_schema(self) -> List[Dict[str, Any]]:
        """Return the config schema for `hermes memory setup`."""
        return [
            {"key": "enabled", "type": "bool", "default": True,
             "description": "Enable the MemPalace memory provider"},
            {"key": "palace_data_dir", "type": "path", "default": "~/.mempalace/palace",
             "description": "ChromaDB data directory"},
            {"key": "mempalace_lib_dir", "type": "path", "default": "~/.openclaw/workspace/mempalace",
             "description": "MemPalace Python package checkout"},
            {"key": "ingestion.mode", "type": "str", "default": "none",
             "description": "each_turn | session_end | none"},
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