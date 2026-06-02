# Changelog

## 1.5.1 (2026-06-02)

### Recent-memory retrieval fix pack

Three root-cause fixes for the "MemPalace forgets what we just worked on" symptom
when natural-language prefetch queries were producing zero hits even though
relevant drawers existed in the palace.

- **Raised `min_score` default 0.3 → 0.5** and added a per-query floor: vague
  natural-language queries (no high-specificity tokens — paths/ports/configs/
  models/quoted) use a relaxed floor (`max(0.25, strict - 0.2)`) so the
  safety-net logic has hits to promote. Token-rich queries keep the strict
  floor. Closes the silent-rejection-of-NL-asks failure mode.
- **Safety-net for vague queries** in `_format_recall_block`: when a query has
  no high-specificity tokens and the classifier found no strong/medium hits,
  the best-scoring hit is promoted to `[strong]` so the model isn't completely
  blind. Classification of all other hits is preserved.
- **Token budget raised** for recall injection: `max_recall_chars` 1800→3500,
  `max_quote_chars_per_hit` 280→320, `max_total_quoted_chars` 1400→2400. Live
  smoke test: 8 hits → 3 injected pre-patch, 8 → 8 post-patch.
- **Recency boost** (`prioritize_recent_days`, default 30, set 0 to disable):
  hits with `created_at`/`date` within the window get a linear additive score
  boost up to +0.10 so fresh drawers win ties with ancient ones. Applied as a
  sort key, not a filter, so old drawers still surface when they're truly the
  best match.

### Internal
- `MemPalaceAPI.search()` now accepts `min_score: Optional[float] = None` and
  falls back to `config.min_score` when not provided. Internal pipeline callers
  in `retrieval.py` pass per-query floors via the new `_score_floor_for()`
  helper.
- `diagnostics()["config"]` now exposes `min_score` and
  `prioritize_recent_days` for operator visibility.
- Tests: 111 passed, 5 skipped (added `prioritize_recent_days = 0` to
  `FakeConfig`; all pre-existing assertions unchanged).

## 1.5.0 (2026-05-28)

### Native MemPalace tool bridge

- Exposed the full 30-tool MemPalace surface natively through the Hermes `MemoryProvider` path via `get_tool_schemas()` and `handle_tool_call()`.
- Reworked `api.py` to call MemPalace library modules directly for palace, graph, diary, sync, hook, and knowledge-graph operations instead of depending on MCP-only wrappers.
- Fixed KG writes to use `KnowledgeGraph.add_triple()` / `invalidate()` and added compatibility fallbacks for older query signatures in tests.
- Added `native_tool_count` to provider diagnostics and updated `plugin.yaml` to advertise the native tool surface.
- Updated operator smoke/docs to assert the native tool bridge instead of the earlier empty-tool contract.

## 1.4.1 (2026-05-22)

### Operator hardening

- Added `CHANGELOG.md` release notes for the modular line (1.4.0 / 1.4.1).
- Added `docs/operator-smoke-test.md` documenting the local validation gate.
- Added `scripts/smoke.sh` to run py_compile, pytest, `hermes memory status`, `hermes mcp test mempalace`, and `load_memory_provider("mempalace")` contract smoke.
- Extended `diagnostics()["metrics"]` with backward-compatible counters: `retrieval_timeouts`, `stale_cache_hits`, `duplicate_hits`, `duplicate_misses`, `chunk_writes`, `l2_recalls`, `l3_searches` (existing keys unchanged).

### Provider contract (included in 56141fd baseline)

- `load_memory_provider()` and `register()` for Hermes memory plugin loading.
- `retrieval_timeout_seconds` on `MemPalaceConfig`.
- Centralized duplicate check in `MemPalaceAPI.add_drawer()`.

## 1.4.0 (2026-05-22)

### Modular refactor

- Split monolithic `__init__.py` into `config.py`, `api.py`, `facts.py`, `retrieval.py`, `provider.py` with thin loader exports.
- Hermes `MemoryProvider` contract: `sync_turn`, empty `get_tool_schemas()`, bounded prefetch/ingest, diagnostics snapshot.
- Default posture: retrieval on; ingestion, fact extraction, KG-in-prefetch, graph, holographic, diary, AAAK, and memory stack off unless opted in.
- Contract test suite expanded (47+ passing with integration skips when palace/MCP unavailable).

## 1.3.0 (2026-05-01)

### New Features
- **KG-assisted recall**: `include_kg_facts` config extracts entity hints from queries and appends knowledge graph triples to prefetch results
- **Graph-assisted prefetch**: `graph.enabled` uses `palace_graph.traverse` and `find_tunnels` to surface connected rooms
- **Agent diary**: `diary.enabled` writes session summaries on end, reads recent entries on start
- **AAAK dialect**: `aaak.enabled` stores lossy compressed digests alongside verbatim drawers (default off)
- **system_prompt_block**: Reports active provider features in the system prompt
- **on_pre_compress**: Extracts structured facts from messages before context compression
- **on_delegation**: Ingests subagent task+result pairs into MemPalace
- **get_config_schema**: Returns config fields for `hermes memory setup`

### Fixes
- `_resolve_kg()` now passes `db_path` derived from `palace_data_dir`
- `_ensure_imported()` uses granular imports so partial failures don't block everything
- `on_session_end()` signature matches ABC (`messages: list`)
- Fact extractor: expanded stop entities (80+ words), 4+ char minimum for single-word entities, fallback verb filter, sentence boundary capture

### Docs
- MCP parity matrix mapping MemoryProvider methods to MCP tools
- CLI operator guide with onboarding, troubleshooting, path mapping
- Updated behavior table with all new features

## 1.2.0 (2026-04-27)

### Initial Release
- Config-activated MemoryProvider (`memory.provider: mempalace`)
- Hybrid search (BM25 + vector) with lexical fallback
- Optional ingestion (per-turn or session-end)
- Knowledge graph integration (add/invalidate triples)
- Memory stack L0-L3 (wake-up, scoped recall, deep search)
- Schema-validated fact extractor
- Background thread tracking and lifecycle management
- Prefetch cache with LRU eviction
- Holographic mirror support
- 22 contract tests
