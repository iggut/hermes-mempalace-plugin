# Changelog

## 1.5.3 (2026-06-02)

### MemPalace dynamics integration (Hebb + Ebbinghaus + Cepeda)

Wires the real `mempalace.dynamics` module into both the plugin's tool
surface and its retrieval sort key. Replaces the hand-rolled recency boost
from v1.5.1 with the production dynamics math (research-grounded: Hebb 1949,
Ebbinghaus 1885, Cepeda 2006) and adds a Hebbian reinforcement path so
frequently-accessed connections naturally rise to the top.

### New tools

- **`mempalace_dynamics_apply`** — Ebbinghaus exponential decay across all
  hall and tunnel connections in one call. Returns aggregate stats:
  `count`, `mean_strength_before`, `mean_strength_after`, `halls_touched`,
  `tunnels_touched`, `now`. Optional `wing` filter only touches that wing's
  halls + tunnels whose source or target is that wing. Optional `now`
  ISO-8601 timestamp for deterministic decay (testing).
- **`mempalace_potentiate`** — Hebbian reinforcement of a single connection
  by ID. Updates `strength` (capped at MAX_STRENGTH=5.0), `last_activated`,
  `access_count`. Grows `stability` if the gap since prior activation is
  at least 1 hour (the Cepeda spacing effect — rapid bursts don't build
  durability). Returns the full updated record.

### Retrieval integration

- New `retrieval.dynamics_enabled` config flag (default **true**) — fail-closed
  helper `_connection_strength_boost_available()` checks the flag plus live
  persistence helpers before enabling.
- New `_compute_connection_boosts(hits)` method on `MemPalaceRetrieval` that
  looks up the live `strength` of every hall/tunnel touching a hit's wing
  and returns a `(wing, room) → boost` map. Combined with the recency boost
  in the sort key — keeps connection effects smaller than recency so age
  still matters.
- New `potentiate_used_connections(hits)` method that, after every
  successful prefetch, calls `potentiate()` on the connections that surfaced
  the hits. Wired into `_fetch_with_timeout` right after the recall block
  is cached; failures are logged and skipped (never breaks the recall path).
- New `dynamics_potentiations` counter in the staged-pipeline diagnostics
  so operators can see how often Hebbian reinforcement fires per session.

### Internal

- `MemPalaceAPI` now lazy-imports `mempalace.dynamics` (apply_decay,
  potentiate, initialize_dynamics_fields) and the halls + tunnels
  persistence helpers (`_load_hallways`, `_save_hallways`,
  `_load_tunnels`, `_save_tunnels`) in `_ensure_imported()`. All wrapped in
  try/except so a missing module is a graceful no-op, not a hard failure.
- `diagnostics()["config"]` now exposes `dynamics_enabled` for operator
  visibility.

### Live verification (2026-06-02)

- `py_compile`: clean on all 6 modules.
- `pytest`: 111 passed, 5 skipped (no regressions; FakeConfig gained the
  new field).
- Unit tests in `/tmp/dynamics_tests.py` cover both new methods + the
  retrieval-side helper. All 6 pass.
- **Hebbian reinforcement verified end-to-end against the live palace**:
  created a test tunnel `memory/conversations → hermes_sessions/ml-inference`
  (initial `strength=1.0, access_count=0`), ran a prefetch with
  `target_wing=memory`, observed `strength: 1.0 → 1.05 (+0.05)` and
  `access_count: 0 → 1 (+1)`. The `dynamics_potentiations` counter
  incremented to 1. Test tunnel deleted after measurement.
- Live recall smoke (8-query battery) — no regression in v1.5.2 numbers;
  the dynamics path is passive in the current palace because none of the
  test query wings are touched by existing halls/tunnels. The wiring is
  active and will fire as soon as matching graph data exists.

## 1.5.2 (2026-06-02)

### Default-on winners from the disabled-feature audit

Live-tested all 14 disabled-by-default flags against the real palace with a
10-query battery. Two features produce measurable gains with zero downside
and are now enabled by default.

- **`always_run_l3: true`** (was `false`): L3 (corpus-wide hybrid search)
  now runs alongside L2 instead of only as a weak-L2 fallback. Live impact:
  `mempalace` 2→8 hits (+1861c), `Hermes` 2→8 hits (+818c). Latency stays
  70-160ms median; zero timeouts across 50+ runs. L2 dedup still collapses
  duplicates, so the L1→L2→L3 flow has no double-counting.

- **`memory_stack_enabled: true`** (was `false`): routes the L1 memory-stack
  recall through MemPalace's real `MemoryStack` (top-importance drawers
  grouped by room). Live impact: +1 L1 hit per query consistently, ~378c
  of structured top-importance context, ~10-20ms latency cost.

- **`wake_up_on_session_start: true`** (was `false`): loads L0 identity + L1
  essentials at session start instead of forcing a first-turn wake that adds
  latency to the first user-visible response.

### Features that stay off (live audit findings)

These flags remain `false` by default. Reasons documented for operators.

- `use_kg` — only fires for capital-letter entities (regex `w[0].isupper()`);
  misses ~90% of natural-language queries. Sometimes REPLACES better
  semantic matches with weaker KG triples (observed: `Jupiter is the agent`
  dropped 8 hits → 3 with KG on). Use only if your queries reliably
  contain proper nouns.
- `holographic_enabled` — `mempalace.holographic` does NOT exist in
  MemPalace 3.3.6. Plugin's `HolographicMirror.ensure_enabled()` returns
  `False`. 100% no-op.
- `aaak_enabled` — `mempalace.aaak` does NOT exist. 100% no-op.
- `use_halls` / `use_closets` — flag is not even referenced in the
  retrieval code path. `mempalace.hallways` exists with real
  `compute_hallways_for_wing`, but the plugin never calls it. Dead code.
- `graph_enabled` / `graph_find_tunnels` / `follow_tunnels` — measurable
  improvement ≈ 0 in the current palace (few cross-wing tunnels in test
  data). Correct, but currently nothing to do.
- `memory_mirror_enabled` — only affects `sync_turn()` and
  `on_memory_write()` (mirrors built-in memory tool writes to MemPalace).
  No read-side impact. Zero effect if you don't use the built-in memory tool.
- `extract_facts_each_turn` — uses regex extraction. Plugin's own CHANGELOG
  documents it as "conservative" (4+ char minimum, 80+ stop entities).
  Off by design until an LLM-based extractor is available.
- `diary_enabled` — roundtrip works (verified write+read, 9 entries in
  palace) but doesn't move recall-block metrics. Use the native MCP
  `mempalace_diary_write`/`_read` tools instead; the MCP path is more
  reliable for cross-session continuity.

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
