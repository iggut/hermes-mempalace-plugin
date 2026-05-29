# MemPalace Feature Parity Audit — 2026-05-28

## MemPalace 3.3.6 Feature Matrix

| Feature | Plugin Status | Notes |
|---|---|---|
| Semantic search | Used | `_api.search()` called in `_fetch_with_timeout` |
| BM25/lexical fallback | Unused | `_lexical_fallback` exists in api.py but not called by retrieval |
| Closets / AAAK | Unused | `get_aaak_spec()`, `get_closet()` in api.py; not wired into retrieval |
| Drawer grep / best chunk | Unused | `drawer_grep()` in api.py; not called |
| Halls / content-type routing | Unused | Not exposed in MemPalace MCP tools |
| Wings / rooms / drawers | Partial | Used in search wing/room params; not used as routing hints for L2 |
| Temporal KG | Partial | `include_kg_facts` triggers `_append_kg_facts` (old method); new `_run_kg_lookup` respects `valid_to` |
| Diary / agent diary | Unused | `diary_write/diary_read` exist in api.py; not called from retrieval |
| Cross-wing tunnels | Unused | `follow_tunnels()` in api.py; wired in new `_run_tunnel_follow` but disabled by default |
| Wake-up / memory stack | Partial | `wake_up_context()` exists in api.py; L0/L1 staged pipeline now implemented |
| MCP tool availability | Reachable | 30 tools via stdio |
| Background/session import | Partial | `avoid_duplicate_session_imports` flag set in config; ingestion checks it |
| Export/status/repair | Partial | `mempalace status` works; repair not wired |
| Duplicate detection | Used | `duplicate_check_enabled` in config; `check_duplicate` called in add drawer |

## Plugin-Local Improvement Opportunities

| Opportunity | Status | Notes |
|---|---|---|
| L0/L1/L2/L3 staged pipeline | Done | New `_run_l0_wake_block`, `_run_l1_mstack`, `_run_l2_scoped_recall`, `_run_l3_hybrid_search` |
| Per-hit quote cap | Done | `max_quote_chars_per_hit` (default 280) |
| Total recall chars cap | Done | `max_recall_chars` (default 1800) |
| Evidence strength labels | Done | `[strong]/[medium]/[weak]` via `_classify_evidence()` |
| Duplicate collapse by drawer_id | Done | In `_fetch_with_timeout` before formatting |
| Lexical exact match boost | Done | Regex patterns for file paths, identifiers, ports in `_classify_evidence` |
| KG expired fact demotion | Done | `valid_to` check in `_run_kg_lookup` |
| Tunnel following | Done | `_run_tunnel_follow` with config caps (disabled by default) |
| Session-scoped cache | Done | Cache key includes session_id |
| Fail-open on import/timeout | Done | `_run_with_timeout` returns default on timeout |
| Diagnostic metrics | Done | `staged_pipeline` section in `diagnostics()` |
| KG header backward compat | Done | `--- Knowledge Graph ---` inserted before first KG hit |
| Backward compat `_fetch_with_timeout` signature | Done | `**kwargs` accepts `timeout=` keyword from old callers |

## Config Fields Added (Phase 3)

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
- `use_halls` — enable hall routing hint (not yet exposed in MemPalace MCP)
- `use_closets` — enable closet-aware routing (not yet wired)
- `avoid_duplicate_session_imports` — guard against double ingestion (default True)

## Not Yet Implemented

- Hall/content-type routing — MemPalace MCP does not expose hall filter tools
- Closet/AAAK-aware search — `get_closet()` exists in api.py but not called from retrieval
- Drawer grep for exact chunk finding — `drawer_grep()` unused
- Diary-based context injection — diary read not wired into L0/L1
- Session importer mode in provider — `avoid_duplicate_session_imports` flag exists but flag wiring incomplete