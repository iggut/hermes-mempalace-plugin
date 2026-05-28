# MemPalace operator smoke test

Local validation gate before calling the plugin a daily driver or starting routing-layer work. Run from any machine with Hermes, the plugin checkout, and (for full green) a MemPalace palace on disk.

## Quick run

```bash
/home/iggut/.hermes/plugins/mempalace/scripts/smoke.sh
```

## Manual gate (same steps as the script)

### 1. Python compile

```bash
PLUGIN=/home/iggut/.hermes/plugins/mempalace
PYTHON="${HERMES_PYTHON:-/home/iggut/.hermes/hermes-agent/venv/bin/python3}"

for f in __init__.py config.py api.py facts.py retrieval.py provider.py; do
  "$PYTHON" -m py_compile "$PLUGIN/$f"
done
```

**Pass:** no output, exit 0.

### 2. Plugin contract tests

```bash
"$PYTHON" -m pytest -q "$PLUGIN/tests"
```

**Pass (this machine baseline):** `47 passed, 5 skipped` (integration tests skip when palace/MCP paths are missing).

**Fail triage:**

- Import errors → check `MEMPALACE_ROOT` / `MEMPALACE_LIB_DIR` and palace path in config.
- Contract failures → read the failing test name; most unit tests use fakes and should not need a live palace.

### 3. Hermes memory status

```bash
hermes memory status
```

**Pass:** provider `mempalace` shows **installed**, **available**, and **active** (when `memory.provider: mempalace` is set).

**Fail triage:**

- Not installed → plugin path not on `HERMES_PLUGIN_PATH` or plugin disabled in config.
- Not available → palace directory missing or MemPalace package not importable (`MEMPALACE_LIB_DIR`).
- Not active → another provider selected in `memory.provider`.

### 4. MCP connectivity

```bash
hermes mcp test mempalace
```

**Pass:** connects and reports ~30 tools (exact count may vary with MemPalace version).

**Fail triage:**

- Connection refused → MCP server not running or wrong server name in Hermes config.
- Auth errors → check MCP env/credentials for the MemPalace server entry.

### 5. `load_memory_provider("mempalace")` smoke

```bash
"$PYTHON" <<'PY'
import sys
sys.path.insert(0, "/home/iggut/.hermes/hermes-agent")
from plugins.memory import load_memory_provider

p = load_memory_provider("mempalace")
assert p is not None
assert p.name == "mempalace"
assert hasattr(p, "sync_turn")
schemas = p.get_tool_schemas()
assert len(schemas) == 30, len(schemas)
assert any(s["name"] == "mempalace_status" for s in schemas)
diag = p.diagnostics()
assert diag["name"] == "mempalace"
assert diag["native_tool_count"] == 30
metrics = diag["metrics"]
for key in (
    "prefetch_cache_hits",
    "retrieval_timeouts",
    "stale_cache_hits",
    "duplicate_hits",
    "duplicate_misses",
    "chunk_writes",
    "l2_recalls",
    "l3_searches",
):
    assert key in metrics, f"missing metric key: {key}"
print("load_memory_provider smoke: OK")
PY
```

**Pass:** prints `load_memory_provider smoke: OK`, exit 0.

**Fail triage:**

- ImportError on `plugins.memory` → run from Hermes agent tree / venv above (`HERMES_AGENT` on `sys.path`).
- Wrong provider class → plugin `__init__.py` export or Hermes loader registration broken.

## Diagnostics counters (1.4.1+)

`provider.diagnostics()["metrics"]` includes legacy keys plus:

| Key | Meaning |
|-----|---------|
| `retrieval_timeouts` | L3 semantic search hit the hard retrieval timeout |
| `stale_cache_hits` | Served an expired cache entry while refreshing |
| `duplicate_hits` | `add_drawer` returned an existing drawer (near-duplicate) |
| `duplicate_misses` | Duplicate check ran, no match, proceeding to write |
| `chunk_writes` | New drawer persisted via `_write_drawer` |
| `l2_recalls` | Memory-stack L2 scoped recall returned text |
| `l3_searches` | Deep hybrid search attempted in retrieval |

Existing keys (`prefetch_cache_*`, `ingest_*`, etc.) are unchanged for backward compatibility.

## Release posture

- **1.4.0** — modular refactor and contract tests.
- **1.4.1** — provider-contract hotfix (56141fd) + operator smoke/docs/diagnostics.

After **1–2 real Hermes sessions** with no regressions, tag this line locally stable. Only then start the routing integration pass (out of scope for 1.4.1).

## What this gate does not cover

- Routing bridge / cross-provider orchestration
- Session-end chat import (separate `mempalace_session_importer` plugin)
- Production load or multi-session soak tests
