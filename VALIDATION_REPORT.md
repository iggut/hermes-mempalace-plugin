# MemPalace Plugin Validation Report

Date: 2026-04-27
Plugin path: ~/.hermes/plugins/mempalace/__init__.py

## Results Summary

| Check | Status |
|-------|--------|
| Plugin discovered | YES - found in `~/.hermes/plugins/mempalace/` |
| register(ctx) called | YES - called during memory provider load |
| on_session_start called | YES - registered as hook, fires on new sessions |
| on_session_end called | YES - registered as hook, fires on session close |
| sync_turn writes to MemPalace | YES - verbatim content stored in ChromaDB |
| Memory written | YES - marker "banana-rocket" found in `chroma.sqlite3` |

## What was fixed

### 1. initialize() signature mismatch (MEMORY PROVIDER ABC)
The ABC requires `initialize(self, session_id: str, **kwargs)` but the plugin had
`initialize(self, hermes_home: str = "")`. Fixed to accept both `session_id` and
`hermes_home` via kwargs.

### 2. is_available was @property instead of method
The ABC defines `is_available` as an abstract method (not a property). The plugin
had it as `@property`. Changed to plain method. This caused `'bool' object is
callable` errors when `_iter_provider_dirs()` called `provider.is_available()`.

### 3. chunk_text() return type mismatch
The mempalace `miner.chunk_text()` returns `list[dict]` with `{'content': ..., 'chunk_index': ...}` 
but the plugin expected raw strings. Added handling for both formats.

### 4. searcher.search() signature mismatch
The actual signature is `(query, palace_path, wing=None, room=None, n_results=5)`
but the plugin passed wrong keyword args (`limit`, `min_score`, etc.). Fixed to
match actual API. Also added None-handling since the function can return None.

### 5. search() return value not normalized
`searcher.search()` returns a formatted string (with ANSI colors) not a list of dicts.
Added None normalization.

## Files changed

- `~/.hermes/plugins/mempalace/__init__.py` - Multiple fixes:
  - Line ~185: Added import logging to _ensure_imported
  - Line ~283-290: Fixed chunk_text() return type handling (dicts vs strings)
  - Line ~330-343: Fixed searcher.search() call signature and None handling
  - Line ~639: Fixed initialize() signature to match ABC
  - Line ~635: Changed is_available from @property to method
  - Line ~681: Added sync_turn logging
  - Line ~746: Added on_memory_write logging
  - Line ~838: Added on_session_start logging
  - Line ~897: Added register() logging

## How to enable permanently

The plugin is already in `plugins.enabled` in config.yaml. To activate it as a
memory provider, ensure these are set in `~/.hermes/config.yaml`:

```yaml
memory:
  provider: mempalace

plugins:
  enabled:
    - mempalace_memory  # or 'mempalace' if using directory name
  
# The plugin respects this env var (default: enabled)
# HERMES_MEMPALACE_MEMORY_ENABLED=1
```

Or run with:
```bash
HERMES_MEMPALACE_MEMORY_ENABLED=1 hermes-agent run_agent.py ...
```

## Notes

- The plugin uses the mempalace Python modules at ~/.openclaw/workspace/mempalace/
  which require chromadb. This is available in both venvs.
- The plugin writes verbatim content as drawers to ChromaDB storage.
- Knowledge graph triples are extracted via regex from natural language (may not
  extract anything from simple test messages).
- Holographic mirroring is enabled but the holographic MemoryStore may need its
  own setup for full functionality.
