#!/usr/bin/env bash
# MemPalace operator smoke gate — exit 0 when all checks pass.
set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_AGENT="${HERMES_AGENT:-$HOME/.hermes/hermes-agent}"
PYTHON="${HERMES_PYTHON:-$HERMES_AGENT/venv/bin/python3}"

if [[ ! -x "$PYTHON" ]]; then
  echo "smoke: missing Python at $PYTHON (set HERMES_PYTHON)" >&2
  exit 1
fi

echo "== MemPalace smoke: py_compile =="
for f in __init__.py config.py api.py facts.py retrieval.py provider.py; do
  "$PYTHON" -m py_compile "$PLUGIN_ROOT/$f"
done

echo "== MemPalace smoke: pytest =="
"$PYTHON" -m pytest -q "$PLUGIN_ROOT/tests"

echo "== MemPalace smoke: hermes memory status =="
hermes memory status

echo "== MemPalace smoke: hermes mcp test mempalace =="
hermes mcp test mempalace

echo "== MemPalace smoke: load_memory_provider =="
"$PYTHON" <<PY
import sys
sys.path.insert(0, "${HERMES_AGENT}")
from plugins.memory import load_memory_provider

provider = load_memory_provider("mempalace")
assert provider is not None
assert provider.name == "mempalace"
assert provider.get_tool_schemas() == []
diag = provider.diagnostics()
assert diag["name"] == "mempalace"
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
    assert key in diag["metrics"], key
print("load_memory_provider smoke: OK")
PY

echo "== MemPalace smoke: ALL PASSED =="
