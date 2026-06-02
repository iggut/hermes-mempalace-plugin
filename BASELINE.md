# MemPalace Plugin Baseline — 2026-05-28

## Plugin state
- Plugin: ~/.hermes/plugins/mempalace  (git: main, clean worktree)
- MemPalace checkout: ~/.openclaw/workspace/mempalace  (3.3.6, clean)
- Palace data: ~/.mempalace/palace
- MemPalace MCP: 30 tools reachable via stdio
- hermes memory status: provider active
- Tests: 54 passed, 5 skipped

## Key gaps identified vs phase-3 requirements

### retrieval.py
- No L0/L1/L2/L3 staged pipeline (flat L2+L3)
- Hardcoded MAX_CHARS=2000, no use of config recall_char_budget
- Hardcoded [:200] and [:500] truncation, no per-hit quote cap
- No exact lexical match boost for file paths, error strings, etc.
- No evidence strength labels ([strong]/[medium]/[weak])
- No duplicate collapse by drawer_id
- No tunnel following with config caps
- No KG invalid fact demotion
- Cache key includes wing/room but not session scope

### config.py
- Missing: max_quote_chars_per_hit, max_total_quoted_chars
- Missing: max_wake_block_chars, max_l2_l3_search_time_ms
- Missing: follow_tunnels, max_tunnel_hops, max_tunnel_hits
- Missing: prefer_active_project, use_kg, use_halls, use_closets
- Missing: avoid_duplicate_session_imports

### api.py / provider.py
- No follow_tunnels wired into retrieval
- No hall filter support in search
- No closet/AAAK-aware routing
- No session importer duplicate guard
- diagnostics() missing capability flags and granular metrics

## MemPalace 3.3.6 features available (via MCP)
- search, wake-up, closets/AAAK, halls, KG, diary, tunnels
- drawer grep via _lexical_fallback
- MCP reachable (30 tools)
- All native tools exposed

## Phase 3 targets
1. Implement L0/L1/L2/L3 staged recall with token/char budgets
2. Add evidence strength labels to recall output
3. Exact lexical match boosting
4. Duplicate collapse by drawer_id
5. Tunnel-following with config caps
6. Config for all token budget parameters

## Patch history
- 2026-05-28: 1.4.0→1.5.0 modular refactor, native 30-tool bridge
- 2026-06-02: 1.5.1 retrieval fix pack (per-query min_score, safety net,
  token-budget raise, recency boost) — closes vague-NL zero-hit symptom.
- 2026-06-02: 1.5.2 default-on winners from feature audit
  (always_run_l3, memory_stack_enabled, wake_up_on_session_start);
  README gained "Feature Audit / Recommended Defaults" section.
