# MemPalace Plugin Gap Analysis

> Generated 2026-06-04. Compares plugin v1.5.3 against official MemPalace package
> at `~/.openclaw/workspace/mempalace`.

## Summary

The plugin has **32 native tools** (30 MCP-parity + 2 dynamics extras) and covers
the core retrieval/ingestion/KG/graph/diary surface. The official package has
**14 Python modules** the plugin doesn't use at all. The most impactful gaps are
in entity detection, palace repair/export, deduplication, and transcript normalization.

---

## Module Coverage Matrix

| Official Module | Plugin Uses? | Impact | Priority |
|---|---|---|---|
| `searcher.py` | ✅ `search_memories` | — | — |
| `palace.py` | ✅ `get_collection` | — | — |
| `miner.py` | ✅ `add_drawer`, `chunk_text` | — | — |
| `knowledge_graph.py` | ✅ `KnowledgeGraph` | — | — |
| `palace_graph.py` | ✅ traverse/tunnels/graph_stats | — | — |
| `hallways.py` | ✅ load/save halls (dynamics) | — | — |
| `dynamics.py` | ✅ apply_decay, potentiate | — | — |
| `layers.py` | ✅ `MemoryStack` (wake_up, recall) | — | — |
| `repair.py` | ⚠️ `status()` only | **scan_palace, prune_corrupt unused** | HIGH |
| `dialect.py` | ⚠️ `Dialect.compress` only | — | LOW |
| `config.py` | ✅ sanitizers | — | — |
| `entity_detector.py` | ❌ Not used | **Sophisticated entity detection unused** | HIGH |
| `entity_registry.py` | ❌ Not used | Entity disambiguation across sessions | MEDIUM |
| `exporter.py` | ❌ Not used | **Palace data export not exposed** | HIGH |
| `dedup.py` | ❌ Not used | **Official dedup module unused** | HIGH |
| `normalize.py` | ❌ Not used | **Transcript normalization unused** | MEDIUM |
| `fact_checker.py` | ❌ Not used | Contradiction detection | LOW (deferred) |
| `llm_refine.py` | ❌ Not used | LLM-powered refinement | LOW (needs LLM) |
| `llm_client.py` | ❌ Not used | Local LLM client | LOW (needs LLM) |
| `convo_miner.py` | ❌ Not used | Conversation transcript mining | MEDIUM |
| `convo_scanner.py` | ❌ Not used | Conversation scanning | LOW |
| `format_miner.py` | ❌ Not used | Format-aware mining | LOW |
| `general_extractor.py` | ❌ Not used | General content extraction | LOW |
| `corpus_origin.py` | ❌ Not used | Corpus origin tracking | LOW |
| `embedding.py` | ❌ Not used | Embedding model management | LOW |
| `onboarding.py` | ❌ Not used | CLI-only | SKIP |
| `hooks_cli.py` | ❌ Not used | CLI-only | SKIP |
| `instructions/` | ❌ Not used | CLI-only | SKIP |
| `instructions_cli.py` | ❌ Not used | CLI-only | SKIP |
| `migrate.py` | ❌ Not used | CLI-only | SKIP |
| `split_mega_files.py` | ❌ Not used | CLI-only | SKIP |
| `project_scanner.py` | ❌ Not used | CLI-only | SKIP |
| `mcp_server.py` | ❌ Not used | Plugin calls Python API directly (better) | SKIP |
| `i18n/` | ❌ Not used | Not relevant | SKIP |

---

## Missing Tools (not in plugin's TOOL_SPECS)

| Tool | Source Module | Description | Priority |
|---|---|---|---|
| `mempalace_repair_scan` | `repair.scan_palace` | Scan palace for corruption/inconsistencies | HIGH |
| `mempalace_repair_prune` | `repair.prune_corrupt` | Remove corrupt drawers | HIGH |
| `mempalace_export` | `exporter.export_palace` | Export palace to markdown/JSON | HIGH |
| `mempalace_dedup_stats` | `dedup.show_stats` | Show deduplication statistics | HIGH |
| `mempalace_dedup_run` | `dedup.dedup_palace` | Run deduplication | HIGH |
| `mempalace_detect_entities` | `entity_detector.detect_entities` | Detect entities in text | MEDIUM |

---

## Implementation Plan

### Phase 17: Repair, Export, Dedup tools
Wire `repair.scan_palace`, `repair.prune_corrupt`, `exporter.export_palace`,
`dedup.show_stats`, `dedup.dedup_palace` into api.py as new tools.

### Phase 18: Entity detection integration
Wire `entity_detector.detect_entities` into facts.py as an alternative
extraction backend. Add `fact_extraction_mode: "entity_detector"` option.

### Phase 19: Transcript normalization
Wire `normalize.normalize` into provider.py ingestion for better handling
of JSONL/JSON transcript formats.

### Phase 20: Documentation
Update ACTION_PLAN.md, README.md, CONFIG_SCHEMA.md, CHANGELOG.md.
