## Learned User Preferences

- Prefer minimal, focused diffs; avoid unrelated refactors or features outside the requested scope.
- Do not create git commits unless the user explicitly asks.
- Before calling a release or daily-driver build ready, run local validation: `py_compile` on plugin modules, `pytest` in `tests/`, `hermes memory status`, `hermes mcp test mempalace`, and `load_memory_provider("mempalace")` smoke from the Hermes agent tree.
- Defer routing-layer integration until the provider has stabilized across one or two real Hermes sessions; keep the plugin thin (Hermes lifecycle in, MemPalace/routing evidence out).
- After provider-contract fixes, favor operator-focused follow-ups (CHANGELOG, operator smoke doc/script, diagnostics counters) over expanding provider-side memory logic.
- When `HERMES_MEMPALACE_MEMORY_ENABLED` is set, users expect it to override only the enabled flag while YAML paths and nested settings still apply; merge, do not replace the gathered config.
- Consolidate improvement backlogs into `ACTION_PLAN.md` rather than maintaining parallel todo files unless the user asks otherwise.

## Learned Workspace Facts

- This repo is the Hermes MemPalace memory plugin at `~/.hermes/plugins/mempalace`; Hermes agent code lives at `~/.hermes/hermes-agent`.
- Hermes integration entry points are `register(ctx)` and `load_memory_provider()`; the provider must subclass `agent.memory_provider.MemoryProvider`, expose `name == "mempalace"`, implement `sync_turn(user_content, assistant_content, ...)`, and expose the native `mempalace_*` tool surface through `get_tool_schemas()` / `handle_tool_call()`.
- Modular layout: `config.py`, `api.py`, `facts.py`, `retrieval.py`, `provider.py`, with `__init__.py` as a thin export/loader layer; `plugin.yaml` advertises `provides_memory_provider: mempalace` plus the native MemPalace tool list.
- Default posture is MemPalace-first: retrieval on; ingestion, fact extraction, KG facts in prefetch, graph, holographic, diary, AAAK, and memory stack off unless opted in.
- Session-end chat import is not owned by the provider lifecycle; the separate `mempalace_session_importer` plugin handles that.
- Config merges `plugins.mempalace`, `plugins.mempalace_memory`, and top-level `mempalace_memory`, plus `memory.provider: mempalace`; env overrides include `MEMPALACE_PALACE_DIR`, `MEMPALACE_LIB_DIR`, and `MEMPALACE_ROOT`.
- The real MemPalace Python package is often on disk under `~/.openclaw/workspace/mempalace` when wiring `MemPalaceAPI` lazy imports.
- Doc-driven backlog for alignment with official MemPalace concepts and MCP/Python/CLI references lives in `ACTION_PLAN.md` Phases 7–16 (Phases 1–6 are the earlier Hermes-provider baseline).
- Operators use two surfaces: this Hermes `MemoryProvider` (prefetch, ingest, mirror) versus the MemPalace MCP server (~30 tools for palace/KG/graph/diary operations).
