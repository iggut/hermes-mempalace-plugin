# MemPalace Plugin Production Readiness Plan

Goal: harden the external Hermes MemPalace MemoryProvider for production use without editing Hermes source.

## Scope

Production-ready means the provider is safe on the hot path, bounded in work, observable, configurable with sane clamps, and verifiable by local tests plus live Hermes/MCP smoke checks.

## Tasks

1. Configuration safety
   - Add explicit bounds for retrieval timeout, result counts, fanout, lexical scan size, prefetch cache size, and thread join timeout.
   - Normalize invalid enum values back to safe defaults.
   - Keep environment overrides backward-compatible.

2. Search safety
   - Keep semantic search primary.
   - Bound lexical fallback scans to a configured maximum rather than unbounded collection reads.
   - Preserve exact drawer-ID lookup before broad lexical scans.
   - Preserve drawer_id and match_type diagnostics.

3. Thread/cache lifecycle
   - Track background ingest/mirror/prefetch threads.
   - Cap prefetch cache size with deterministic oldest-entry eviction.
   - Avoid starting duplicate prefetches for the same session/query.
   - Join tracked threads briefly on shutdown/session end without blocking indefinitely.

4. Diagnostics and metrics
   - Add in-process counters for searches, fallbacks, cache hits/misses, timeouts, ingestion attempts/errors, duplicate skips, KG invalidation attempts, and shutdown joins.
   - Add a provider diagnostics snapshot method and expose it through status tooling.

5. Tests
   - Add provider-local contract tests for config clamps, lexical scan limit, prefetch cache eviction, metrics snapshot, and shutdown join behavior.
   - Run the existing contract suite after each implementation step.

6. Documentation
   - Update README, CONFIG_SCHEMA, plugin metadata, and this plan with production defaults and verification commands.

7. Verification
   - Syntax compile plugin/importer.
   - Run provider contract tests.
   - Smoke-load provider and diagnostics.
   - Run `hermes memory status`.
   - Run `hermes mcp test mempalace`.
