# Changelog

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
