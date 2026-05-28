---
name: mempalace-plugin-api-alignment
version: 1.1
author: jupiter
created_at: "2026-04-27T00:00:00Z"
description: Fix the MemPalace Hermes plugin by comparing each API call in ~/.hermes/plugins/mempalace/ against the actual signatures in /home/iggut/.openclaw/workspace/mempalace/mempalace/*.py, then patching mismatches.
---

# MemPalace Plugin API Signature Alignment

## When to Use

When the MemPalace library (`/home/iggut/.openclaw/workspace/mempalace/mempalace/`) has evolved and the Hermes plugin (`~/.hermes/plugins/mempalace/`) is calling functions with wrong parameters, missing parameters, or expecting wrong return types. The plugin is now modular (not a single `__init__.py`).

**Plugin module structure:**
```
mempalace/
  __init__.py     # Thin export layer
  config.py       # MemPalaceConfig dataclass, load_config()
  api.py          # MemPalaceAPI — all read/write ops and native tool bridge (fail-open)
  facts.py        # SchemaValidatedFactExtractor for KG
  retrieval.py    # MemPalaceRetrieval — hybrid search, cache, timeout executor
  provider.py     # MemPalaceMemoryProvider — Hermes MemoryProvider ABC
  tests/test_mempalace_provider_contract.py  # contract tests
```

**v1.5+ native tool bridge:**
- `provider.get_tool_schemas()` must delegate to `MemPalaceAPI.get_tool_schemas()`
- `provider.handle_tool_call()` must delegate to `MemPalaceAPI.handle_tool_call()` and return JSON-safe results
- `plugin.yaml` should advertise the native `mempalace_*` tool surface so Hermes can introspect it without MCP discovery
- provider diagnostics should expose `native_tool_count` so smoke tests can assert parity quickly

**Internal attribute names (v1.4+):**
| Old name | v1.4+ name | Used in |
|----------|-----------|---------|
| `self._miner` | `self._miner_add_drawer_fn` | `api.py` `_write_drawer()` |
| `self._searcher` | `self._search_memories_fn` | `api.py` `search()` |
| `self._kg` | `self._kg` (same) | `api.py` KG methods |
| `self._palace` | `self._palace` (same) | `api.py` palace ops |
| `self._col` | `self._col` (same) | `api.py` collection ops |
| `self._chunk_text_fn` | `self._chunk_text_fn` (same) | `api.py` `chunk_text()` |
| `_lexical_fallback()` | `_lexical_fallback()` (same, now on api) | `api.py` |
| N/A | `self._retrieval` (MemPalaceRetrieval) | `provider.py` |

**Important: test mocking uses different attribute names than implementation.**
Tests set `api._search_memories_fn = FakeSearcher()` — the actual implementation wraps this behind the scenes. When patching tests, use the test-level attribute names (the ones tests assign directly), not the internal wrapper names.

## Core Pattern (4 steps)

### Step 1: List all API calls in the plugin and their signatures

Search the plugin for every call to `self._miner.*`, `self._searcher.*`, `self._kg.*`, `self._palace.*`:

```bash
grep -n "self\._\(miner\|searcher\|kg\|palace\)\\." /home/iggut/.hermes/plugins/mempalace/__init__.py
```

Then check each library function's signature:

```bash
/home/iggut/.openclaw/workspace/mempalace-venv/bin/python -c "
import sys, inspect
sys.path.insert(0, '/home/iggut/.openclaw/workspace/mempalace')
from mempalace import miner, searcher, knowledge_graph, palace

# Each function the plugin calls:
for name, func in [
    ('miner.add_drawer', miner.add_drawer),
    ('miner.chunk_text', miner.chunk_text),
    ('searcher.search_memories', searcher.search_memories),
    ('KG.add_triple', knowledge_graph.KnowledgeGraph.add_triple),
    ('KG.query_entity', knowledge_graph.KnowledgeGraph.query_entity),
]:
    print(f'{name}: {inspect.signature(func)}')
"
```

### Step 2: Compare each call against the actual signature

For each plugin method (e.g., `MemPalaceAPI.add_drawer`, `MemPalaceAPI.chunk_and_add`, `MemPalaceAPI.search`, `MemPalaceAPI.kg_add_triple`, `MemPalaceAPI.kg_query`), verify:
- All required positional/keyword params match the library signature
- Return types match expectations (list of dicts vs raw strings, dict with "results" key vs None)
- The correct function is called (e.g., `searcher.search_memories()` NOT `searcher.search()`)

### Step 3: Check for common pitfalls

| Pitfall | Symptom | Fix |
|---------|---------|-----|
| `searcher.search()` instead of `searcher.search_memories()` | prefetch returns "" every turn | Use `search_memories()` which returns structured dict |
| Wrong param order or missing params in `add_drawer()` | Drawer not persisted, silent failure | Match exact signature from miner.py |
| `chunk_text()` returns list of dicts with 'content' key but plugin expects raw strings | TypeError on len() or iteration | Handle both formats: check if items are dicts |
| `KG.add_triple()` needs entity auto-creation | KG queries return empty | Library auto-creates entities; verify caller passes correct params |
| KG writes still call old helper names like `kg.add(...)` | KG write path explodes at runtime | Call `KnowledgeGraph.add_triple()` and `KnowledgeGraph.invalidate()` directly |
| `KnowledgeGraph.query_entity()` signature differs across environments | tests pass in one venv and fail in another with `TypeError` on `as_of` | Add a compatibility fallback that retries without `as_of` when unsupported |
| `palace.get_collection()` requires create=False for read-only | Collection created when not expected | Use `create=False` for search/status operations |
| Provider bridge works but palace path is corrupt | native tool smoke fails with SQLite/collection errors and the plugin gets blamed | validate against a known-good backup palace path before concluding provider/API drift

### Step 4: Apply fixes and verify

Each fix:
1. Edit the plugin with `patch(old_string, new_string)`
2. Run `python3 -m py_compile <plugin_path>/__init__.py` to check syntax
3. Run a full smoke test (see below)

## Verification Checklist

After all fixes:

1. `python3 -m py_compile ~/.hermes/plugins/mempalace/__init__.py` — syntax OK
2. Each plugin method calls the correct library function with correct params
3. Return types match expectations (list/dict vs None)
4. `[MemPalaceMemory] initialized lazily:` log line is present
5. `initialize()` calls `self._mp_api._ensure_imported()` so modules are ready before any prefetch() call
6. `is_available()` is filesystem-only (no `_ensure_imported()` call, no DB operations)
7. `prefetch()` has a timeout/fail-fast path using `self._config.retrieval_timeout_seconds`
8. `load_memory_provider('mempalace')` reports the expected native tool count through diagnostics
9. `provider.get_tool_schemas()` returns the full expected native tool list
10. `provider.handle_tool_call()` successfully dispatches at least one real tool (for example `mempalace_get_aaak_spec`)
11. If the default palace path is unhealthy, rerun native tool smoke against a known-good backup palace path to separate storage corruption from bridge regressions

## End-to-End Smoke Test

Run this after every fix session:

```bash
/home/iggut/.openclaw/workspace/mempalace-venv/bin/python -c "
import sys, os, time
sys.path.insert(0, '/home/iggut/.hermes/plugins/mempalace')
from __init__ import load_config, MemPalaceAPI, MemPalaceMemoryProvider

cfg = load_config()
provider = MemPalaceMemoryProvider(cfg)
provider.initialize(session_id='test')

# Search test
results = provider._mp_api.search('Hermes', limit=3, min_score=0.3)
assert len(results) > 0, 'Search returned no results'

# Add drawer test
did = provider._mp_api.add_drawer(
    content='SMOKE TEST: plugin is working.',
    wing='test', room='smoke_test', source_file='smoke.py',
    chunk_index=0, agent='smoke-test'
)
assert did is not None, 'add_drawer returned None'

# KG test
tid = provider._mp_api.kg_add_triple('SmokeTest', 'tests', 'MemPalace plugin')
assert tid is not None, 'kg_add_triple returned None'

# Prefetch test (with timeout)
start = time.time()
prefetch_result = provider.prefetch('Hermes', session_id='test')
elapsed = time.time() - start
assert elapsed < 3.0, f'Prefetch took too long: {elapsed}s'

provider.shutdown()
print('ALL TESTS PASSED')
"
```

## Pitfalls

- The library path is `/home/iggut/.openclaw/workspace/mempalace`, NOT the plugin path `~/.hermes/plugins/mempalace`
- The venv with chromadb is at `/home/iggut/.openclaw/workspace/mempalace-venv/bin/python` — use this for all signature checks
- `_ensure_imported()` must be called at the END of `initialize()` (after constructing MemPalaceAPI) so modules are ready before any prefetch() call races against the timeout
- The timeout thread in prefetch must be `daemon=True` so it doesn't clean up shutdown
- `_run_with_timeout()` must wrap both `executor.submit(fn)` and `future.result(...)` in the same `try` block; during Hermes CLI interpreter shutdown, `ThreadPoolExecutor.submit()` can raise `RuntimeError: cannot schedule new futures after interpreter shutdown`, and that should fail open to `None` instead of printing a background traceback
- Add a regression test that monkeypatches the retrieval executor to raise that exact `RuntimeError` so quiet CLI smoke runs stay quiet on exit
- Don't change the library itself — only modify the plugin
- `chunk_text()` returns a list of dicts with `'content'` key; if the plugin treats items as raw strings, add a check: `if isinstance(chunks[0], dict): chunk_texts = [c.get('content', '') for c in chunks]`

## Protocol

1. `python3 -m pytest ~/.hermes/plugins/mempalace/ -q` — confirm contract tests pass
2. `git -C ~/.hermes/plugins/mempalace log --oneline -10` — check new upstream commits
3. Compare plugin API calls against the live MemPalace library signatures in the active venv
4. Verify both surfaces separately:
   - MCP transport health (`hermes mcp test mempalace`)
   - Hermes-native provider bridge (`load_memory_provider('mempalace')`, tool count, one real `handle_tool_call()` smoke)
5. Run smoke test from the SKILL.md
6. If the bridge is healthy but the default palace path errors, validate against a known-good backup store before diagnosing provider drift
7. If all pass with no mismatches → plugin is current, no action needed

## Compatibility Reference

After every MemPalace update, run the compatibility check and file it under `references/`:

```
references/v<version>-compatibility-check.md
```

If the update requires a broader provider/tool-bridge audit, store that note under a more specific name and link it here.

A completed check with no mismatches means no plugin changes needed. See:
- `references/v1.3.0-compatibility-check.md` — pre-modular refactor
- `references/v1.4-compatibility-check.md` — modular refactor baseline
- `references/v3.3.6-native-tool-bridge-check.md` — native Hermes tool bridge rollout, KG API drift, and backup-palace validation pattern

## Related Skills

- `mempalace-plugin-slow-init-fix` — covers initialization performance fixes (lazy imports, timeouts, filesystem-only checks) for the same file
- `hermes-mempalace-integration` — broader integration audit covering MCP setup, search priority, and diagnostic sequences