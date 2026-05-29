# MemPalace Feature Parity Audit — 2026-05-28 (post Phase 4 review)

## MemPalace 3.3.6 Feature Matrix

| Feature | Plugin Status | Notes |
|---|---|---|
| Semantic search | Used | `_api.search()` called in L2 and L3 stages |
| BM25/lexical fallback | Used | `_lexical_fallback` in `api.search()` — called automatically when semantic results < limit |
| Closets / AAAK | Unused | `get_aaak_spec()`, `get_closet()` in api.py; not wired into retrieval |
| Drawer grep / best chunk | Unused | No `drawer_grep()` method in api.py; `list_drawers()` exists but unused |
| Halls / content-type routing | No-op | Not exposed in MemPalace MCP tools; `use_halls` is a config-only flag |
| Wings / rooms / drawers | Partial | Used in search wing/room params; not used as routing hints for L2 |
| Temporal KG | Partial | `_run_kg_lookup` respects `valid_to`; `include_kg_facts` legacy flag still works |
| Diary / agent diary | Unused | `diary_write/diary_read` exist in api.py; not called from retrieval |
| Cross-wing tunnels | Wired (disabled) | `_run_tunnel_follow` with config caps; `follow_tunnels: false` default |
| Wake-up / memory stack | Fixed | L0 now correctly calls `wake_up_context(wing, char_budget)` instead of missing `wake_up()` |
| MCP tool availability | Reachable | 30 tools via stdio |
| Background/session import | Partial | `avoid_duplicate_session_imports` now wired in `on_delegation()` |
| Export/status/repair | Partial | `mempalace status` works; repair not wired |
| Duplicate detection | Used | `duplicate_check_enabled` in config; `check_duplicate` called in add drawer |

## Phase 4 Review Fixes Applied

| Issue | Fix |
|---|---|
| L0 called wrong method `wake_up()` | Now calls `wake_up_context(wing, char_budget)` — the real API; falls back to `wake_up` attr if absent |
| Lexical patterns too broad | Replaced with `_extract_query_tokens()` — extracts CONCRETE tokens (path strings, identifiers, ports, model slugs, config keys, quoted substrings); hit is strong ONLY when SAME extracted token appears in content |
| L3 always runs unconditionally | L3 now runs ONLY when L2 finds zero strong/medium hits; `always_run_l3: false` default |
| Duplicate guard flag unwired | `on_delegation()` now skips writes when `ingestion_mode=session_end` and `avoid_duplicate_session_imports=True` |
| No L2.5 exact-match step | Added `_run_l2_exact_match()` — extracts specific tokens from query, runs targeted search with tight 150ms timeout and 2-result cap |

## Config Fields Added (Phase 3 + Phase 4 review)

- `max_wake_block_chars` — L0 cap (default 600, range [100, 5000])
- `max_recall_chars` — total recall block cap (default 1800, range [200, 8000])
- `max_quote_chars_per_hit` — per-hit content cap (default 280, range [50, 2000])
- `max_total_quoted_chars` — aggregate quoted chars cap (default 1400, range [100, 5000])
- `max_l3_search_time_ms` — L3 timeout (default 400, range [50, 2000])
- `follow_tunnels` — enable cross-wing tunnel following (default False)
- `max_tunnel_hops` — max tunnel hops (default 1, range [1, 5])
- `max_tunnel_hits` — max tunnel hits returned (default 2, range [1, 10])
- `prefer_active_project` — prefer wing/room scoping for active project (default True)
- `use_kg` — enable KG lookup in L2 (default False; `include_kg_facts` still works for legacy)
- `use_halls` — enable hall routing hint (not yet exposed in MemPalace MCP — no-op)
- `use_closets` — enable closet-aware routing (not yet wired — no-op)
- `avoid_duplicate_session_imports` — guard against double ingestion (default True)
- `always_run_l3` — force L3 to run even when L2 finds signal (default False)

## No-op / Unavailable Features

These features are documented as config options but are currently non-functional:

| Feature | Reason |
|---|---|
| `use_halls` | MemPalace MCP does not expose hall filter tools |
| `use_closets` | `get_closet()` exists in api.py but retrieval.py does not call it |
| Drawer grep | No `drawer_grep()` in api.py; `list_drawers()` exists but is not used for retrieval routing |
| Diary in L0/L1 | Diary read not wired into retrieval pipeline |
| Session importer mode | Flag exists but session-end importer integration is incomplete |

## Retrieval Pipeline (post Phase 4 review)

```
L0: wake_up_context(wing, char_budget=max_wake_block_chars)  — always attempted
L1: scoped_recall(wing, room, char_budget=recall_char_budget)  — if memory_stack_enabled
L2:
  L2.5: _run_l2_exact_match() — token-extracted exact query, 150ms, 2 results max
  KG:   _run_kg_lookup() — if use_kg or include_kg_facts
  Tunnels: _run_tunnel_follow() — if follow_tunnels
  Semantic: _search(wing, room) — if wing known
L3: _run_l3_hybrid_search() — ONLY if L2 found zero strong+medium hits AND always_run_l3=false
Format: _format_recall_block() — char caps, per-hit caps, total quoted chars cap, [strong]/[medium] labels
```