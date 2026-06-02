# MemPalace Memory Plugin

Automated memory provider for Hermes Agent backed by MemPalace verbatim drawers, hybrid search, and optional knowledge-graph / Holographic mirroring.

## Module Structure

```
mempalace/
  __init__.py     # Thin export layer: load_plugin(), MemPalaceConfig, MemPalaceAPI, MemPalaceMemoryProvider
  config.py       # MemPalaceConfig dataclass, load_config(), YAML/env/arg parsing
  api.py          # MemPalaceAPI: all read/write operations (fail-open)
  facts.py        # SchemaValidatedFactExtractor for KG
  retrieval.py    # MemPalaceRetrieval: hybrid search, cache TTL, timeout executor
  provider.py     # MemPalaceMemoryProvider: Hermes MemoryProvider ABC implementation
  tests/
    test_mempalace_provider_contract.py  # 38 contract tests (all passing)
```

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
| Memory stack (L0–L3) | enabled by default since v1.5.2 | Real `mempalace.layers.MemoryStack`: bounded `wake_up()` (L0+L1) on session start; L2 `recall()` when wing/room are known; L3 corpus-wide hybrid search runs alongside L2. |
| Living connections (dynamics) | enabled by default since v1.5.3 | Real `mempalace.dynamics` (Hebb + Ebbinghaus + Cepeda): connection `strength` boosts the sort key; every successful recall potentiates the connections that surfaced its hits. Admin tools: `mempalace_dynamics_apply`, `mempalace_potentiate`. |
| KG-assisted recall | disabled | `include_kg_facts: true` extracts entity hints from queries and appends knowledge graph triples to prefetch. |
| Graph-assisted prefetch | disabled | `graph.enabled: true` uses `palace_graph.traverse` / `find_tunnels` to surface connected rooms in prefetch. |
| Agent diary | disabled | `diary.enabled: true` writes session summaries on end, reads recent entries on start. |
| AAAK dialect | disabled | `aaak.enabled: true` stores lossy compressed digests alongside verbatim drawers. Never replaces retrieval. |
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
| `retrieval.always_run_l3` | `true` | Always run corpus-wide L3 search alongside L2. (See [Feature Audit](#feature-audit--recommended-defaults).) |
| `retrieval.dynamics_enabled` | `true` | Wire real MemPalace connection dynamics into the sort key + Hebbian reinforcement. (See [Living Connections](#living-connections-v153).) |
| `memory_stack.enabled` | `true` | Use real MemPalace `MemoryStack` for L0+L1 wake and L2 scoped recall. |
| `memory_stack.wake_up_on_session_start` | `true` | Load L0 identity + L1 essentials at session start. |
| `retrieval.timeout_ms` | `500` | Hard budget for synchronous fallback retrieval. |
| `facts.extract_each_turn` | `false` | Explicitly opt into regex fact extraction. |
| `holographic.enabled` | `false` | Optional Holographic fact mirror. (Module missing in MemPalace 3.3.6 — no-op.) |
| `memory_mirror.enabled` | `false` | Optional mirroring of Hermes built-in memory writes. |

## Feature Audit / Recommended Defaults

Every disabled-by-default flag in this plugin was live-tested against the
real palace at `~/.mempalace/palace` on 2026-06-02 with a 10-query battery
(isolated runs, fresh session-id per query to defeat cache). The full audit
script is at `scripts/feature_audit.py` (run from Hermes 3.11 venv, do NOT
inject the 3.12 mempalace-venv). Audit scripts are kept in `/tmp` for
follow-up runs.

### Turn ON (default since v1.5.2) — measurable gains, zero downside

#### `retrieval.always_run_l3: true`

L3 (corpus-wide hybrid search) was gated to only run when L2 found nothing
strong/medium. That guard works well for keyword-rich queries but silently
missed short-token queries like `mempalace` and `Hermes` (only 2 hits
returned) where L2's scoped search has nothing to anchor against.

Live impact (clean isolated runs):

| Query | off (hits/chars) | on (hits/chars) | gain |
|---|---|---|---|
| `mempalace` | 2 / 1199c | 8 / 3060c | +6 hits, +1861 chars |
| `Hermes` | 2 / 958c | 8 / 1776c | +6 hits, +818 chars |
| `RPGP` | 6 / 3159c | 6 / 3159c | (no change — already strong) |
| `narrator prompt builder` | 8 / 3365c | 8 / 3365c | (no change — L2 wins) |

Median latency stays 70-160ms per query, well under the 500ms
`retrieval_timeout_ms` budget. Zero timeouts across 50+ runs. L2 dedup in
`_fetch_with_timeout` collapses duplicates, so the L1→L2→L3 flow has no
double-counting. **Set `always_run_l3: false` only if you want minimum-cost
corpus-wide suppression.**

#### `memory_stack.enabled: true` + `memory_stack.wake_up_on_session_start: true`

Routes the L1 memory-stack recall through MemPalace's real `MemoryStack`
(top-importance drawers grouped by room, ~500-800 tokens). Without these
flags, L1 always returns 0 because the `scoped_recall` API is gated on
`memory_stack_enabled`.

Live impact (per-query gain, consistent across all 10 test queries):

| Query | off (L1 hits/chars) | on (L1 hits/chars) | gain |
|---|---|---|---|
| `mempalace` | 0 / 1199c | 1 / 1577c | +1 hit, +378 chars |
| `Hermes` | 0 / 958c | 1 / 1336c | +1 hit, +378 chars |
| `narrator prompt builder` | 0 / 3365c | 1 / 3325c | +1 hit, replaces a lower-relevance |
| `Igor preferences` | 0 / 1825c | 1 / 2203c | +1 hit, +378 chars |
| `what did we work on recently` | 0 / 780c | 1 / 1015c | +1 hit, +235 chars |
| `RPGP` | 0 / 3159c | 1 / 3117c | +1 hit, replaces a lower-relevance |

Latency cost is +10-20ms median. The L1 pass surfaces drawers that are
*important but not semantically similar* to the query — exactly the context
type that "what was I working on" questions need. **The two flags
(`enabled` and `wake_up_on_session_start`) work as a pair**: turning on
`enabled` without `wake_up_on_session_start` forces a first-turn wake that
adds latency to the first user-visible response.

### Leave OFF (default) — live audit found no value or broken implementation

#### `use_kg: false` — only fires for capital-letter entities

The KG extraction in `retrieval.py:781-785` filters entities with
`len(w) > 2 and w[0].isupper()`. So `Igor`, `Jupiter`, `RPGP` work, but
most natural-language queries have zero capital-first words → KG lookup is
a no-op. And when it *does* fire, it sometimes REPLACES better semantic
matches with weaker KG triples:

| Query | off (hits) | on (hits) | Δ |
|---|---|---|---|
| `Igor and Jupiter` | 8 | 9 | +1 |
| `Jupiter is the agent` | 8 | 3 | **−5** |
| `Did Igor do this work` | 1 | 6 | +5 |
| `mempalace plugin` | 7 | 7 | 0 |
| `what did Igor do recently` | 8 | 6 | −2 |

**Don't enable** unless your queries reliably contain proper nouns.
If you do enable it, the entity filter regex in `retrieval.py` is the first
thing to patch.

#### `holographic_enabled: false` — module missing

`mempalace.holographic` does not exist in MemPalace 3.3.6. The plugin's
`HolographicMirror._check_available()` returns `False` and
`ensure_enabled()` returns `False`. **100% no-op.** Enabling it just adds
log noise about a feature that never initializes.

#### `aaak_enabled: false` — module missing

`mempalace.aaak` does not exist. The plugin has the config flag, the
loader, the schema docs — but no implementation. The package has
`dialect.py` (a different thing). **100% no-op.**

#### `use_halls: false` / `use_closets: false` — unwired dead code

`use_halls` is not even referenced in the retrieval code path — there's
no branch reading it. The MemPalace package has `hallways.py` (not
`halls.py`) with `compute_hallways_for_wing`, `list_hallways`, etc., but
the plugin never calls them. `use_closets` similarly has no wiring. Both
are documentation ghosts; enabling them does literally nothing.

#### `graph_enabled` / `graph_find_tunnels` / `follow_tunnels` — no measurable gain

All three were toggled in isolation. Total hits/chars/latency were
statistically indistinguishable from baseline. The palace has 700+ drawers
but few cross-wing tunnels in the test data, so these features are correct
but currently have nothing to do. **Re-test after more cross-wing content
accumulates.**

#### `memory_mirror_enabled: false` — writes only, no read impact

Verified byte-identical `prefetch()` output with mirror off vs on. The
feature only affects `sync_turn()` and `on_memory_write()` — it mirrors
built-in `memory` tool writes to the MemPalace palace. **If you don't use
the built-in memory tool, this is a no-op.** If you do, it gives you a
duplicate-store. The plugin doc itself recommends it stay off unless you
need the dual-write.

#### `extract_facts_each_turn: false` — regex lossy, off by design

Uses `SchemaValidatedFactExtractor` (regex-based) to extract
`(subject, predicate, object)` triples from conversation turns. The
plugin's own CHANGELOG documents it as "conservative" (4+ char minimum,
80+ stop entities, sentence-boundary capture) — i.e., tuned to avoid
garbage. Off by default and should stay off until an LLM-based extractor
is available.

#### `diary_enabled: false` — works, but use MCP instead

The diary roundtrip works (verified write+read against live palace, 9
entries already in `wing_jupiter`). But enabling it through the provider
config doesn't change recall-block metrics. The native MCP
`mempalace_diary_write` / `mempalace_diary_read` tools are the more
reliable path for cross-session continuity. Enable this flag only if you
want auto-diary-on-session-end behavior, not as a recall boost.

### Recommended config (the minimal "better/faster/stronger" yaml)

```yaml
mempalace_memory:
  enabled: true
  retrieval:
    always_run_l3: true            # TIER 1: corpus-wide L3 alongside L2
  memory_stack:
    enabled: true                  # TIER 1: real MemoryStack L0+L1
    wake_up_on_session_start: true # TIER 1: L0 identity at session start
```

That's the full set of recommended flips. Leave everything else at
defaults unless you have a specific need documented above.

## Living Connections (v1.5.3+)

This plugin wires MemPalace's real connection dynamics into the retrieval
pipeline. Two new tools expose the math; the retrieval path uses it
automatically.

### The math

Research-grounded in `mempalace/dynamics.py` (Hebb 1949, Ebbinghaus 1885,
Cepeda 2006):

- **Hebbian potentiation** — when a connection is used, it gets stronger.
  `strength` grows by `POTENTIATION_INCREMENT` (0.05) per co-access, capped
  at `MAX_STRENGTH` (5.0). Tuned so ~20 co-accesses bring a fresh connection
  to max.
- **Ebbinghaus exponential decay** — unused connections fade with time
  since last activation. `new = old * exp(-days_since_last / stability)`,
  floored at `STRENGTH_FLOOR` (0.05). The palace doesn't forget, salience
  just drops.
- **Cepeda spacing effect** — stability grows by `STABILITY_INCREMENT` (0.1)
  only when the gap since the prior activation is at least
  `SPACED_INTERVAL_HOURS` (1.0). Bursts of rapid co-access don't build
  durability; distributed practice does.

### New tools

- **`mempalace_dynamics_apply`** — Ebbinghaus decay across all halls and
  tunnels. Returns `{count, mean_strength_before, mean_strength_after,
  halls_touched, tunnels_touched, now}`. Optional `wing` filter and `now`
  ISO-8601 timestamp. Pure admin operation — call periodically (daily cron
  is a good default).
- **`mempalace_potentiate`** — Hebbian reinforcement of a single connection
  by ID. Returns the full updated record. Use `kind: 'hall' | 'tunnel'`.

### Automatic integration

When `retrieval.dynamics_enabled: true` (default), every successful prefetch:

1. Looks up the live `strength` of every hall/tunnel touching a hit's wing
   and adds a small additive boost to the sort key (capped at 0.05, so age
   still matters more than reinforcement).
2. Calls `potentiate()` on the connections that surfaced the hits
   (Hebbian reinforcement). The `dynamics_potentiations` counter in
   diagnostics tracks how often this fires.

The hand-rolled recency boost from v1.5.1 is still active in parallel —
dynamics effects are smaller than recency effects, so the user's age
preference is preserved.

### Cron suggestion

For long-running installs:

```bash
# Apply Ebbinghaus decay to all connections once per day
0 3 * * * hermes mcp call mempalace_dynamics_apply '{}' >/dev/null 2>&1
```

### Verified end-to-end

Live test (2026-06-02, against `~/.mempalace/palace`): created a test
tunnel `memory/conversations → hermes_sessions/ml-inference` (initial
`strength=1.0, access_count=0`), ran a prefetch with `target_wing=memory`,
observed `strength: 1.0 → 1.05 (+0.05)` and `access_count: 0 → 1 (+1)`. The
`dynamics_potentiations` counter incremented to 1. Test tunnel deleted
after measurement.

## Two Surfaces: MemoryProvider vs MCP

The MemPalace memory plugin provides two integration surfaces:

1. **MemoryProvider** (this plugin) — automated, lifecycle-integrated. The agent calls `prefetch()`, `sync_turn()`, `on_memory_write()` transparently via the Hermes MemoryManager.
2. **MCP tools** — 30 explicit tools exposed by `mempalace mcp` for direct agent/tool use (search, drawers, KG, diary, graph, tunnels, etc.).

### Parity Matrix

| MemoryProvider method | MCP equivalent | Notes |
|---|---|---|
| `prefetch()` | `mempalace_search` | Provider does hybrid search + KG + graph; MCP is search-only |
| `sync_turn()` | `mempalace_add_drawer` | Provider auto-chunks and extracts facts; MCP is manual |
| `on_memory_write()` | `mempalace_add_drawer` / `mempalace_kg_invalidate` | Provider mirrors automatically |
| `queue_prefetch()` | (none) | Provider-only background warming |
| `on_delegation()` | `mempalace_add_drawer` | Provider ingests subagent results automatically |
| `on_pre_compress()` | (none) | Provider extracts facts before compression |
| (none) | `mempalace_traverse` | MCP-only graph traversal; provider uses `graph.enabled` config |
| (none) | `mempalace_create_tunnel` | MCP-only explicit tunnel creation |
| (none) | `mempalace_dynamics_apply` | Admin: Ebbinghaus decay across all halls + tunnels. (See [Living Connections](#living-connections-v153).) |
| (none) | `mempalace_potentiate` | Hebbian reinforcement of a single hall or tunnel by ID. |
| (none) | `mempalace_diary_write` / `mempalace_diary_read` | Provider has `diary.enabled`; MCP is always available |
| (none) | `mempalace_compress` | MCP-only AAAK compression CLI; provider has `aaak.enabled` |

Use the MemoryProvider for automated, zero-config memory. Use MCP tools for explicit, agent-initiated operations (creating tunnels, manual diary writes, ad-hoc compression).

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

## CLI Operator Guide

### Onboarding

```bash
# 1. Initialize a MemPalace
mempalace init ~/.mempalace/palace

# 2. (Optional) Mine existing content into the palace
mempalace mine ~/.mempalace/palace

# 3. (Optional) Generate L0+L1 wake-up context
mempalace wake-up ~/.mempalace/palace

# 4. Start MCP server (for direct tool access)
mempalace mcp

# 5. Configure Hermes
# Add to ~/.hermes/config.yaml:
#   memory:
#     provider: mempalace
#   mempalace_memory:
#     enabled: true
#     palace_data_dir: ~/.mempalace/palace
```

### Troubleshooting

```bash
# Check palace status
mempalace status ~/.mempalace/palace

# Repair a corrupted palace
mempalace repair ~/.mempalace/palace

# Check plugin diagnostics
hermes memory status

# Test MCP connectivity
hermes mcp test mempalace

# Check effective config
python -c "
import sys; sys.path.insert(0, '$HOME/.hermes/plugins/mempalace')
from __init__ import load_config
cfg = load_config()
print(f'enabled={cfg.enabled}, palace={cfg.palace_data_dir}')
print(f'retrieval={cfg.retrieval_mode}, stack={cfg.memory_stack_enabled}')
print(f'diary={cfg.diary_enabled}, aaak={cfg.aaak_enabled}')
"
```

### Path Mapping

| CLI flag | Hermes config | Environment variable |
|----------|--------------|---------------------|
| `--palace` | `mempalace_memory.palace_data_dir` | `MEMPALACE_PALACE_DIR` |
| (auto) | `mempalace_memory.mempalace_lib_dir` | `MEMPALACE_LIB_DIR` / `MEMPALACE_ROOT` |

### Session Importer

The background session importer runs on `on_session_end`:

```bash
# Disable
HERMES_ENABLE_MEMPALACE_SESSION_IMPORTER=0

# Custom importer path
HERMES_MEMPALACE_IMPORTER=/path/to/importer.py
```

## Verification Commands

Operator gate (compile, pytest, Hermes status, MCP test, provider load): see [docs/operator-smoke-test.md](docs/operator-smoke-test.md) or run:

```bash
~/.hermes/plugins/mempalace/scripts/smoke.sh
```

## Bundled Optional Skill

This repo ships the maintainer skill used to audit and repair MemPalace↔Hermes API drift:

- `optional-skills/mempalace-plugin-api-alignment/SKILL.md`

The skill includes its compatibility notes under the adjacent `references/` directory so the workflow can travel with the repo instead of living only in a local Hermes profile.

Quick manual checks:

```bash
python -m py_compile ~/.hermes/plugins/mempalace/__init__.py
/home/iggut/.hermes/hermes-agent/venv/bin/python3 -m pytest -q ~/.hermes/plugins/mempalace/tests/test_mempalace_provider_contract.py
hermes memory status
hermes mcp test mempalace
```

## Diagnostics

The provider exposes `diagnostics()` which returns a snapshot of internal state:

```python
provider = load_plugin()
print(provider.diagnostics())
```

Key fields:
- `enabled`, `initialized`, `available` — provider state
- `session_id` — current session
- `prefetch_cache_size`, `prefetch_cache_limit` — cache utilization
- `prefetch_inflight` — active background prefetch threads
- `background_threads` — tracked background workers
- `metrics` — counters: prefetch cache hits/misses/evictions, retrieval timeouts, stale cache hits, duplicate hits/misses, chunk writes, L2 recalls, L3 searches, ingest attempts/errors (see [docs/operator-smoke-test.md](docs/operator-smoke-test.md))
- `memory_stack_enabled`, `wake_block_chars` — memory stack state

## Future Improvements

1. Replace regex fact extraction with explicit, schema-validated LLM extraction before enabling by default.
2. Migrate the provider-local tests into the upstream Hermes repo if this plugin is upstreamed.
3. Add a first-class Hermes admin command to report effective MemPalace config and diagnostics.
4. Doc-driven backlog (memory stack L0–L3, real Python API wiring, KG in prefetch, MCP parity, CLI onboarding): see `ACTION_PLAN.md` **Phases 7–16** and **Official MemPalace reference URLs** there.