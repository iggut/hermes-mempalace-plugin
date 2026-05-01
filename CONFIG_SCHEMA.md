# MemPalace Memory Plugin — Configuration Schema

Add to `~/.hermes/config.yaml`.

## Minimal activation

```yaml
memory:
  provider: mempalace
```

`memory.provider: mempalace` activates the provider by default. Environment variable overrides are still supported:

- `HERMES_MEMPALACE_MEMORY_ENABLED=1|true|yes|on` forces the provider **enabled** flag on
- `HERMES_MEMPALACE_MEMORY_ENABLED=0|false|no|off` forces the provider **enabled** flag off
- These variables override only `enabled`; the rest of `mempalace_memory` / `plugins.mempalace*` YAML (paths, `memory_stack`, retrieval, etc.) is still loaded and merged.

## Full schema

```yaml
memory:
  provider: mempalace

mempalace_memory:
  enabled: true

  # Paths. If omitted, the plugin auto-detects sane local defaults.
  palace_data_dir: ~/.mempalace/palace          # ChromaDB data directory containing chroma.sqlite3
  mempalace_lib_dir: ~/.openclaw/workspace/mempalace  # MemPalace Python package checkout
  # Compatibility aliases accepted by the loader:
  # palace_path: ~/.mempalace/palace
  # lib_path: ~/.openclaw/workspace/mempalace

  ingestion:
    mode: none                 # "each_turn" | "session_end" | "none"
    min_turn_length: 20        # Skip turns shorter than this char count
    max_turn_length: 8000      # Reserved cap for long turns
    chunk_size: 800            # Chars per drawer chunk
    chunk_overlap: 100         # Overlap between chunks
    wing: memory               # Target wing for conversation drawers
    room: conversations        # Target room
    agent: jupiter             # added_by field value

  facts:
    extract_each_turn: false   # Conservative default; enable only when KG quality is acceptable
    min_confidence: 0.7
    max_facts_per_turn: 10

  retrieval:
    enabled: true
    mode: hybrid               # "vector" | "bm25" | "hybrid"
    vector_weight: 0.6
    bm25_weight: 0.4
    max_results: 8
    min_score: 0.3
    include_kg_facts: true
    kg_entity_limit: 5
    timeout_ms: 500

  holographic:
    enabled: false
    default_trust: 0.5

  memory_mirror:
    enabled: false
    mirror_add: true
    mirror_replace: true
    mirror_remove: true        # Requires concrete kg_triple metadata to invalidate KG facts
    target_wing: memory

  performance:
    background_ingest: true
    background_retrieval: true
    timeout_ms: 500            # Also maps to retrieval timeout; clamped to 50..10000
    max_fanout: 10             # Clamped to 1..100
    prefetch_cache_size: 32    # Per-provider queued recall cache; clamped to 1..1000
    lexical_scan_limit: 1000   # Max rows inspected by lexical fallback; clamped to 10..5000
    thread_join_timeout_ms: 1000  # Shutdown/session-end join budget; clamped to 0..5000

  # Optional MemPalace memory stack (L0–L3); requires mempalace.layers.MemoryStack
  memory_stack:
    enabled: false
    wake_up_on_session_start: false   # L0+L1 via MemoryStack.wake_up() at session start
    wake_up_on_first_turn: false       # If session-start wake did not run, wake on turn 0/1
    wake_up_wing: ""                    # Optional wing passed to wake_up (empty = library default)
    l2_room: ""                        # Default room for L2 scoped recall when no room in metadata
    l2_before_deep_search: true         # Run L2 recall before L3 when scope is known
    l2_skip_deep_search_when_recall_non_empty: false  # If true, skip L3 when L2 returns text
    identity_path: ""                 # Optional path to L0 identity file (~/.mempalace/identity.txt)
    wake_char_budget: 3200             # Clamp wake-up text length (chars)
    recall_char_budget: 1500          # Clamp L2 recall text length (chars)
    recall_n_results: 10               # Passed to MemoryStack.recall when supported

  # Optional graph-assisted prefetch (Phase 9)
  graph:
    enabled: false                     # Enable graph traversal in prefetch
    max_hops: 2                        # BFS hop limit for traverse (clamped 1..5)
    limit: 10                          # Max graph nodes returned (clamped 1..50)
    find_tunnels: false                # Also find cross-wing tunnels in prefetch
```

`prefetch()` / `queue_prefetch()` accept optional `prefetch_wing` / `prefetch_room` (or `wing` / `room` in kwargs) so callers can supply session metadata for L2. L3 remains hybrid `search_memories` + lexical fallback (deep semantic search).

## Environment path overrides

The loader also checks these variables before path auto-detection:

```bash
MEMPALACE_PALACE_DIR=~/.mempalace/palace
MEMPALACE_PALACE_PATH=~/.mempalace/palace
MEMPALACE_ROOT=~/.openclaw/workspace/mempalace
MEMPALACE_LIB_DIR=~/.openclaw/workspace/mempalace
```

## Notes

- Retrieval is designed to be prompt-safe and bounded to about 2000 characters.
- Config values are normalized into safe bounds at load time; invalid retrieval/ingestion modes fall back to `hybrid`/`none`.
- Lexical fallback scans are bounded by `performance.lexical_scan_limit` after exact drawer-ID lookup.
- `queue_prefetch()` warms a capped per-session cache; `prefetch()` consumes the cached result when available and records cache hit/miss/timeout counters.
- Background ingest, mirror, and prefetch work is tracked and briefly joined on shutdown/session end within a single global join budget.
- `performance.background_retrieval: false` disables queued retrieval and makes `prefetch()` run its bounded fallback inline.
- `ingestion.max_turn_length` is enforced before chunking.
- The provider exposes `diagnostics()` and `mempalace_status()` includes `provider_diagnostics` when invoked through the plugin context.
- Automatic ingestion and fact extraction are intentionally opt-in to prevent accidental transcript or noisy KG growth.
- Direct drawer writes perform a near-duplicate check before writing and return an existing drawer ID when a duplicate is detected.
- Search uses semantic MemPalace results first, then a deterministic lexical fallback over drawer IDs, source paths, wing/room metadata, and a short document prefix. The fallback normalizes punctuation, so `using-superpowers`, `using_superpowers`, and `using superpowers` resolve equivalently when present in drawer metadata/content.
- L0 identity is normally `~/.mempalace/identity.txt` inside MemPalace; set `memory_stack.identity_path` to override when your stack supports it.
- Remove mirroring does not infer deprecations from free text. To invalidate a KG fact, pass metadata such as:

```python
{
  "kg_triple": {
    "subject": "Max",
    "predicate": "does",
    "object": "chess",
    "ended": "2026-01-01"
  }
}
```

## Session-end importer plugin

The optional focused session-end importer is separate from this MemoryProvider:

```yaml
plugins:
  mempalace_session_importer:
    enabled: true
```

Runtime environment overrides:

```bash
HERMES_ENABLE_MEMPALACE_SESSION_IMPORTER=0
HERMES_MEMPALACE_IMPORTER=~/.hermes/scripts/hermes_chat_importer.py
```
