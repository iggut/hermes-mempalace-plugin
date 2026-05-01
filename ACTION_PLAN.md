# MemPalace Memory Plugin Improvement Action Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development to implement this plan task-by-task. Use TDD for behavior changes.

**Goal:** Make the Hermes MemPalace MemoryProvider reliable, config-driven, low-latency, and aligned with the healthy MCP integration.

**Architecture:** Keep the official MemPalace MCP path as the stable, update-safe integration. Harden the optional MemoryProvider plugin so `memory.provider: mempalace` activates it predictably, recall is cached rather than hot-path blocking, and writes are conservative/non-duplicative.

**Tech Stack:** Hermes Agent Python plugin API, `agent.memory_provider.MemoryProvider`, Hermes `config.yaml`, MemPalace direct Python API/MCP, pytest-compatible smoke scripts.

**Backlog:** Phases 7–16 consolidate the official MemPalace doc–driven improvement list (concepts + reference); execute after Phases 1–6 unless you are patching regressions.

---

## Phase 1: Activation and config correctness

**Files:**
- Modify: `/home/iggut/.hermes/plugins/mempalace/__init__.py`
- Modify: `/home/iggut/.hermes/plugins/mempalace/README.md`
- Modify: `/home/iggut/.hermes/plugins/mempalace/CONFIG_SCHEMA.md`
- Verify: `/home/iggut/.hermes/config.yaml`

- [x] Treat `memory.provider: mempalace` as sufficient activation.
- [x] Make `HERMES_MEMPALACE_MEMORY_ENABLED=0|false|no` an explicit disable override.
- [x] Make `HERMES_MEMPALACE_MEMORY_ENABLED=1|true|yes` an explicit enable override.
- [x] Parse plugin config from `mempalace_memory`, `plugins.mempalace_memory`, or `plugins.mempalace` when present.
- [x] Remove hardcoded user paths from dataclass defaults and resolve paths at runtime.
- [x] Keep safe runtime defaults: retrieval enabled, per-turn ingestion off, memory mirror off unless configured.

## Phase 2: Provider contract hardening

**Files:**
- Modify: `/home/iggut/.hermes/plugins/mempalace/__init__.py`

- [x] Subclass `agent.memory_provider.MemoryProvider` when available.
- [x] Keep a fallback base class for direct import/test contexts where Hermes is not importable.
- [x] Rename the provider turn-start lifecycle to `on_turn_start` while keeping `on_session_start` as a compatibility alias.
- [x] Ensure session switch resets cached per-session state safely.

## Phase 3: Low-latency recall

**Files:**
- Modify: `/home/iggut/.hermes/plugins/mempalace/__init__.py`

- [x] Implement `queue_prefetch(query, session_id)` to warm recall in a background thread.
- [x] Make `prefetch(query, session_id)` return cached recall quickly when available.
- [x] Keep a bounded synchronous fallback using `retrieval_timeout_ms` / `retrieval_timeout_seconds`.
- [x] Format recalled memories with stable metadata and drawer IDs when available.

## Phase 4: Safer writes

**Files:**
- Modify: `/home/iggut/.hermes/plugins/mempalace/__init__.py`

- [x] Include `session_id`, turn number, and content hash in per-turn source IDs.
- [x] Surface real MemPalace drawer IDs in logs/metadata when `miner.add_drawer()` exposes them through `chunk_and_add()`.
- [x] Keep regex KG extraction available but conservative and disabled by default.
- [x] Add duplicate-check integration using MemPalace's duplicate API or local manifest.
- [x] Convert remove mirroring to real KG invalidation when a concrete triple is available.

## Phase 5: Documentation cleanup

**Files:**
- Modify: `/home/iggut/.hermes/plugins/mempalace/README.md`
- Modify: `/home/iggut/.hermes/plugins/mempalace/CONFIG_SCHEMA.md`
- Modify: `/home/iggut/.hermes/plugins/mempalace/plugin.yaml`

- [x] Align docs with runtime defaults.
- [x] Document MCP and MemoryProvider as separate health paths.
- [x] Clarify that direct tool functions are internal helpers unless tool schemas are implemented.
- [x] Split the useful session-end importer hook out of `hybrid_mempalace_compress` into a small focused plugin.

## Phase 6: Verification

Run these commands after each implementation pass:

```bash
python -m py_compile /home/iggut/.hermes/plugins/mempalace/__init__.py
PYTHONPATH=/home/iggut/.hermes/hermes-agent /home/iggut/.hermes/hermes-agent/venv/bin/python3 - <<'PY'
from plugins.memory import load_memory_provider
p = load_memory_provider('mempalace')
assert p is not None
assert p.name == 'mempalace'
print('available', p.is_available())
print('has queue_prefetch', hasattr(p, 'queue_prefetch'))
PY
/home/iggut/.local/bin/hermes memory status
/home/iggut/.local/bin/hermes mcp test mempalace
```

Expected:
- Python compile exits 0.
- Provider loads as `mempalace`.
- `hermes memory status` reports provider installed and available on this machine.
- `hermes mcp test mempalace` discovers tools.

## Phase 7: Real MemPalace integration and code integrity

**Files:** `/home/iggut/.hermes/plugins/mempalace/__init__.py`, tests as needed.  
**Sources:** [Python API](https://mempalaceofficial.com/reference/python-api.html), [API reference](https://mempalaceofficial.com/reference/api-reference.html).

- [x] Wire `MemPalaceAPI` to the real MemPalace package (replace stubs): lazy-import from `mempalace_lib_dir` / `MEMPALACE_LIB_DIR`, fail soft with clear logs when import or palace path missing.
- [x] Search path: use `mempalace.searcher.search_memories` with `palace_path`, optional `wing` / `room`, `n_results`; map result fields (`text`, `similarity`, `wing`, `room`, `source_file`) to the provider internal shape.
- [x] KG path: use `mempalace.knowledge_graph.KnowledgeGraph` for add/invalidate; pass `source_file` / `source_closet` when linking facts to drawers.
- [x] Fix `HolographicMirror`: `close()` must not reference undefined `_store` (implement or no-op stub).
- [x] Fix prefetch cache generation: `_prefetch_generation` / `prefetch_generation` on config — implement or remove; align with session reset.
- [x] Complete `initialize()` for Hermes lifecycle (imports, `_mp_api` readiness, no race with first `prefetch()` under timeout).
- [x] Align dataclass defaults with README / `CONFIG_SCHEMA.md` (`extract_facts_each_turn` vs `facts.extract_each_turn`, `fact_extraction_mode`); single mapping in `load_config()`.

## Phase 8: Memory stack (L0–L3)

**Sources:** [Memory stack](https://mempalaceofficial.com/concepts/memory-stack.html), `mempalace.layers.MemoryStack`.

- [x] Session wake-up (L0 + L1): optional `MemoryStack().wake_up(wing=...)` on session start or first turn; bounded token budget (~600–900).
- [x] L2 scoped recall: when metadata provides wing/room, call `recall` before or instead of corpus-wide deep search.
- [x] L3: document current `prefetch()` as deep semantic search; keep hybrid/vector path.
- [x] Config: `memory_stack.enabled`, `wake_up_on_session_start`, `wake_up_wing`, `l2_room`, char/token caps.
- [x] Document `~/.mempalace/identity.txt` (L0); optional `identity_path` if supported by `MemoryStack`.

## Phase 9: Palace metadata, graph, tunnels

**Sources:** [The Palace](https://mempalaceofficial.com/concepts/the-palace.html), [MCP navigation / palace_graph](https://mempalaceofficial.com/reference/mcp-tools.html).

- [x] Forward configured or session-derived `wing` / `room` on every search call.
- [x] Optional graph-assisted prefetch: `palace_graph.traverse` / `find_tunnels` behind a flag; strict hop/result limits.
- [x] Document halls (`hall_facts`, `hall_events`, …) vs miner metadata; note `mempalace init` / `mine` for hall-rich corpora.
- [ ] Document MCP-only tunnel tools unless product requires tunnel-aware recall in Hermes-only mode.

## Phase 10: Knowledge graph in recall and contradiction follow-up

**Sources:** [Knowledge graph](https://mempalaceofficial.com/concepts/knowledge-graph.html), [Contradiction detection](https://mempalaceofficial.com/concepts/contradiction-detection.html).

- [x] Implement `include_kg_facts`: entity hints from query, `query_entity` / `timeline` within `kg_entity_limit`, append to prefetch within char budget.
- [x] Triple provenance on ingest: `source_file`, `source_closet` on `add_triple`.
- [ ] Contradiction checks: defer until upstream ships stable API/MCP; then optional `contradiction_check.enabled` or document MCP-only workflow.
- [x] Tests: KG append + invalidate against temp DB (skip if `mempalace` not installed).

## Phase 11: Specialist agents and diary

**Sources:** [Agents](https://mempalaceofficial.com/concepts/agents.html), [MCP diary tools](https://mempalaceofficial.com/reference/mcp-tools.html).

- [ ] Document stable `ingestion.agent` / `agent_name` ↔ `wing_<name>` convention.
- [ ] Optional diary: config `diary.enabled`, `diary.agent_name`; session-end append if Python API exists, else document MCP `mempalace_diary_write`.
- [ ] Optional diary read on session start (`diary.last_n`, bounded tokens).

## Phase 12: AAAK dialect (optional)

**Source:** [AAAK dialect](https://mempalaceofficial.com/concepts/aaak-dialect.html).

- [ ] Default off: no AAAK on stored drawers or default prefetch (document R@5 tradeoff).
- [ ] Optional `Dialect.compress` for digests only; strict limits; never replace verbatim default retrieval.
- [ ] Document `mempalace compress` CLI vs Hermes runtime.

## Phase 13: MCP parity and Hermes surface docs

**Source:** [MCP tools](https://mempalaceofficial.com/reference/mcp-tools.html).

- [ ] Expand README “two surfaces”: MemoryProvider vs 29 MCP tools.
- [ ] Maintainer parity matrix: provider methods ↔ MCP tool names.
- [ ] Verify duplicate-check before `add_drawer` / `chunk_and_add` when MemPalace duplicate API is wired.
- [ ] Document `mempalace_reconnect` when CLI and Hermes share a palace.

## Phase 14: CLI operator workflow

**Source:** [CLI](https://mempalaceofficial.com/reference/cli.html).

- [ ] Onboarding: `mempalace init` → optional `mine` / `wake-up` → `mempalace mcp` → Hermes `memory.provider: mempalace` + paths.
- [ ] Incident docs: `mempalace repair`, `mempalace status`; `split` + `mempalace_session_importer`.
- [ ] Document CLI `--palace` ↔ Hermes `palace_data_dir`.

## Phase 15: Fact extraction quality

- [ ] Audit `SchemaValidatedFactExtractor` regexes (fix broken fragments); safe default `none` until LLM extraction exists.
- [ ] Optional validated LLM extraction when Hermes supports a safe callout.
- [x] Wire `allowed_predicates` from config into extractor.

## Phase 16: Diagnostics, tests, packaging

- [ ] Surface `diagnostics()` / effective config via `hermes memory status` or doc until upstream seam exists.
- [ ] Optional integration test: real `mempalace` + fixture palace (skipped by default).
- [ ] Load/concurrency: prefetch latency, thread join budget.
- [ ] `plugin.yaml` version/description; optional `CHANGELOG.md` / README release notes.

### Execution order (Phases 7–16)

1. Phase 7 (real API + bugfixes).  
2. Phase 8 (memory stack).  
3. Phases 10 + 15 (KG in prefetch, extraction safety).  
4. Phases 9, 11, 12 (optional flags and docs).  
5. Phases 13–14 (documentation).  
6. Phase 16 (polish); Phase 13 overlaps README edits.

### Official MemPalace reference URLs

| Topic | URL |
|--------|-----|
| Palace | https://mempalaceofficial.com/concepts/the-palace.html |
| Contradiction detection | https://mempalaceofficial.com/concepts/contradiction-detection.html |
| Knowledge graph | https://mempalaceofficial.com/concepts/knowledge-graph.html |
| Agents | https://mempalaceofficial.com/concepts/agents.html |
| AAAK | https://mempalaceofficial.com/concepts/aaak-dialect.html |
| Memory stack | https://mempalaceofficial.com/concepts/memory-stack.html |
| Python API | https://mempalaceofficial.com/reference/python-api.html |
| API reference | https://mempalaceofficial.com/reference/api-reference.html |
| MCP tools | https://mempalaceofficial.com/reference/mcp-tools.html |
| CLI | https://mempalaceofficial.com/reference/cli.html |

## Deferred improvements

- Replace direct Python API calls with an MCP-backed provider mode for stronger dependency isolation.
- Keep provider-local pytest suite synced with runtime behavior and migrate it into the upstream Hermes repo if this plugin is upstreamed.
- Add a `hermes memory doctor mempalace` style diagnostic command if Hermes exposes a suitable plugin CLI seam.
