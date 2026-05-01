# MemPalace Memory Plugin

Automated memory provider for Hermes Agent backed by MemPalace verbatim drawers, hybrid search, and optional knowledge-graph / Holographic mirroring.

## Current Behavior

| Feature | Default | Description |
|---------|---------|-------------|
| Provider activation | `memory.provider: mempalace` | Config activates the provider without requiring an environment variable. |
| Environment override | optional | `HERMES_MEMPALACE_MEMORY_ENABLED=0/false/no/off` disables; `1/true/yes/on` enables. |
| Auto-retrieval | enabled | `prefetch()` injects bounded MemPalace recall before model calls. |
| Queued retrieval | enabled by implementation | `queue_prefetch()` warms a capped per-session, per-query cache for the next turn. |
| Production bounds | enabled | Config values are clamped to safe ranges; lexical fallback scans, cache size, and thread join time are bounded. |
| Diagnostics | enabled | Provider exposes an in-process diagnostics snapshot with cache/thread state and counters. |
| Auto-ingest | disabled | `ingestion.mode: none` by default to avoid unexpected verbatim transcript writes. |
| Fact extraction | disabled | Conservative default to avoid noisy KG triples. Enable explicitly. |
| Memory mirroring | disabled | Built-in `memory` tool writes are only mirrored when configured. |
| Holographic mirroring | disabled | Optional overlay, disabled by default. |
| Duplicate safety | enabled for direct drawer writes | `add_drawer()` checks MemPalace for near-duplicates before writing and returns the existing drawer ID on a hit. |
| Lexical fallback | enabled for search | If semantic search misses or has spare result slots, exact drawer IDs and skill/source-file name variants are matched deterministically. |
| Memory stack (L0–L3) | disabled | Optional `mempalace.layers.MemoryStack`: bounded `wake_up()` (L0+L1) on session or first turn; L2 `recall()` when wing/room are known; L3 remains `prefetch()` hybrid search. |
| Session-end import | separate plugin | `mempalace_session_importer` owns the background chat importer hook; compression remains separate. |

## Activation

Preferred config:

```yaml
memory:
  provider: mempalace

mempalace_memory:
  enabled: true
  retrieval:
    enabled: true
```

Explicit temporary overrides:

```bash
HERMES_MEMPALACE_MEMORY_ENABLED=1 hermes memory status
HERMES_MEMPALACE_MEMORY_ENABLED=0 hermes memory status
```

The plugin auto-detects:

- Palace data directory: `~/.mempalace/palace` when it contains `chroma.sqlite3`
- MemPalace library checkout: `~/.openclaw/workspace/mempalace` when it contains the Python package

You can override paths with config or environment variables:

- `mempalace_memory.palace_data_dir`
- `mempalace_memory.mempalace_lib_dir`
- `MEMPALACE_PALACE_DIR` / `MEMPALACE_PALACE_PATH`
- `MEMPALACE_ROOT` / `MEMPALACE_LIB_DIR`

## Configuration

See `CONFIG_SCHEMA.md` for the full schema. Safe defaults prioritize recall without automatic memory pollution:

| Option | Default | Description |
|--------|---------|-------------|
| `ingestion.mode` | `none` | `each_turn`, `session_end`, or `none`. |
| `retrieval.enabled` | `true` | Search MemPalace before model calls. |
| `retrieval.timeout_ms` | `500` | Hard budget for synchronous fallback retrieval. |
| `facts.extract_each_turn` | `false` | Explicitly opt into regex fact extraction. |
| `holographic.enabled` | `false` | Optional Holographic fact mirror. |
| `memory_mirror.enabled` | `false` | Optional mirroring of Hermes built-in memory writes. |

## Provider Integration Notes

- `MemPalaceMemoryProvider` subclasses Hermes `MemoryProvider` when imported inside Hermes.
- `register(ctx)` always registers the provider; availability is determined by config and path checks.
- `initialize(session_id=...)` stores the active session ID and warms MemPalace imports.
- `sync_turn()` uses session-aware source names like `session_<id>_turn_<n>`.
- `on_memory_write()` uses session-aware source names like `session_<id>_memory_add_user` when metadata contains a session ID.
- `on_memory_write(action="remove")` only performs KG invalidation when metadata includes a concrete `kg_triple` / `triple` object with `subject`, `predicate`, and `object`.
- Direct drawer writes return real MemPalace drawer IDs when the backend exposes them, or a deterministic fallback ID otherwise.
- `search()` first uses MemPalace semantic search, then fills remaining result slots with lexical matches over drawer IDs, source paths, wing/room metadata, and a short document prefix.
- Lexical matching normalizes hyphens, underscores, spaces, and punctuation, so queries like `context-surfing`, `context_surfing`, and `context surfing` can resolve the same skill/source drawer.
- `queue_prefetch()` caches by `(session_id, query, prefetch_wing, prefetch_room)`; `prefetch()` consumes cached results first and falls back to a timeout-bounded L3 search. Optional `memory_stack` config enables L0+L1 wake-up and L2 scoped recall ahead of L3. If `performance.background_retrieval` is false, `queue_prefetch()` is a no-op and `prefetch()` performs the bounded search inline without tracked background threads.
- Background ingest, mirror, and retrieval threads are tracked and joined within a global shutdown/session-end budget.
- `sync_turn()` enforces `ingestion.max_turn_length` before chunking so unexpectedly large turns cannot enter the ingestion path unbounded.

## Session-End Importer Plugin

The focused importer hook lives at `~/.hermes/plugins/mempalace_session_importer`.
It launches `~/.hermes/scripts/hermes_chat_importer.py` in the background on `on_session_end`.

Environment overrides:

```bash
HERMES_ENABLE_MEMPALACE_SESSION_IMPORTER=0   # disable the hook
HERMES_MEMPALACE_IMPORTER=/path/to/importer.py
```

## Verification Commands

```bash
python -m py_compile ~/.hermes/plugins/mempalace/__init__.py
/home/iggut/.hermes/hermes-agent/venv/bin/python3 -m pytest -q ~/.hermes/plugins/mempalace/tests/test_mempalace_provider_contract.py
hermes memory status
hermes mcp test mempalace
```

## Future Improvements

1. Replace regex fact extraction with explicit, schema-validated LLM extraction before enabling by default.
2. Migrate the provider-local tests into the upstream Hermes repo if this plugin is upstreamed.
3. Add a first-class Hermes admin command to report effective MemPalace config and diagnostics.
4. Doc-driven backlog (memory stack L0–L3, real Python API wiring, KG in prefetch, MCP parity, CLI onboarding): see `ACTION_PLAN.md` **Phases 7–16** and **Official MemPalace reference URLs** there.