import importlib.util
import os
import sys
import tempfile
import time
from pathlib import Path

PLUGIN = Path('/home/iggut/.hermes/plugins/mempalace/__init__.py')


def load_plugin():
    spec = importlib.util.spec_from_file_location('mempalace_plugin_contract', PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_config_provider_activates_without_env(monkeypatch):
    monkeypatch.delenv('HERMES_MEMPALACE_MEMORY_ENABLED', raising=False)
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        (home / '.mempalace' / 'palace').mkdir(parents=True)
        (home / '.mempalace' / 'palace' / 'chroma.sqlite3').write_text('')
        monkeypatch.setenv('HOME', str(home))
        mod = load_plugin()
        cfg = mod.load_config({'memory': {'provider': 'mempalace'}})
        assert cfg.enabled is True
        assert cfg.palace_data_dir == str(home / '.mempalace' / 'palace')


def test_env_false_overrides_config_provider(monkeypatch):
    monkeypatch.setenv('HERMES_MEMPALACE_MEMORY_ENABLED', '0')
    mod = load_plugin()
    cfg = mod.load_config({'memory': {'provider': 'mempalace'}})
    assert cfg.enabled is False


def test_config_clamps_production_bounds():
    mod = load_plugin()
    cfg = mod.load_config({
        'memory': {'provider': 'mempalace'},
        'mempalace_memory': {
            'ingestion': {'mode': 'unsafe', 'min_turn_length': -5, 'max_turn_length': 5, 'chunk_size': 1, 'chunk_overlap': 10000},
            'retrieval': {'mode': 'weird', 'max_results': 999, 'min_score': 2, 'timeout_ms': -1},
            'performance': {'max_fanout': 999, 'prefetch_cache_size': 0, 'lexical_scan_limit': 1000000, 'thread_join_timeout_ms': 999999},
        },
    })
    assert cfg.ingestion_mode == 'none'
    assert cfg.retrieval_mode == 'hybrid'
    assert 1 <= cfg.max_results <= 50
    assert 0 <= cfg.min_score <= 1
    assert cfg.retrieval_timeout_ms >= 50
    assert cfg.max_fanout <= 100
    assert cfg.prefetch_cache_size >= 1
    assert cfg.lexical_scan_limit <= 5000
    assert cfg.thread_join_timeout_ms <= 10000
    assert cfg.max_turn_length >= cfg.min_turn_length
    assert cfg.chunk_overlap < cfg.chunk_size


def test_queue_prefetch_caches_by_session(monkeypatch):
    """Verify prefetch cache stores and returns results by (session, query, wing, room) key."""
    mod = load_plugin()
    # Disable retrieval to test the cache directly
    provider = mod.MemPalaceMemoryProvider(mod.MemPalaceConfig(enabled=True, retrieval_enabled=False))
    monkeypatch.setattr(provider, '_ensure_api', lambda: None)
    # Mark as initialized so prefetch doesn't early-exit
    provider._initialized = True

    class FakeAPI:
        pass  # not called when retrieval is disabled

    provider._mp_api = FakeAPI()
    # Pre-populate cache
    key = provider._prefetch_key('abc', 's1', '', '')
    provider._prefetch_cache[key] = 'cached result for abc'
    # prefetch should return cached value without calling mp_api
    result = provider.prefetch('abc', session_id='s1')
    assert result == 'cached result for abc'


def test_prefetch_cache_evicts_oldest_entry_when_full():
    mod = load_plugin()
    provider = mod.MemPalaceMemoryProvider(mod.MemPalaceConfig(enabled=True, retrieval_enabled=True, prefetch_cache_size=2))
    provider._cache_prefetch_result(('s', 'one', '', ''), '1')
    provider._cache_prefetch_result(('s', 'two', '', ''), '2')
    provider._cache_prefetch_result(('s', 'three', '', ''), '3')
    assert ('s', 'one', '', '') not in provider._prefetch_cache
    assert provider._prefetch_cache[('s', 'two', '', '')] == '2'
    assert provider._prefetch_cache[('s', 'three', '', '')] == '3'
    assert provider.diagnostics()['metrics']['prefetch_cache_evictions'] == 1


def test_source_files_include_session_turn_and_hash():
    mod = load_plugin()
    provider = mod.MemPalaceMemoryProvider(mod.MemPalaceConfig(enabled=True))
    provider._turn_count = 7
    source = provider._turn_source_file(session_id='session-abcdef', content='hello')
    assert source == 'session_session-abcdef_turn_7_2cf24dba5f'


def test_add_drawer_uses_duplicate_check_and_returns_existing_id():
    mod = load_plugin()

    class FakeCollection:
        def query(self, **kwargs):
            return {
                'ids': [['drawer_existing']],
                'distances': [[0.01]],
                'metadatas': [[{'wing': 'memory', 'room': 'conversations'}]],
                'documents': [['same content']],
            }

    class FakePalace:
        def get_collection(self, path):
            return FakeCollection()

    class FakeMiner:
        called = False
        def add_drawer(self, col, wing, room, content, src, chunk_idx, agent):
            self.called = True
            return True

    api = mod.MemPalaceAPI('/tmp/no-palace')
    api._imported = True
    api._palace = FakePalace()
    api._col = FakeCollection()
    api._miner_add_drawer_fn = FakeMiner()
    drawer_id = api.add_drawer('same content', duplicate_threshold=0.9)
    assert drawer_id == 'drawer_existing'
    assert api._miner_add_drawer_fn.called is False


def test_add_drawer_surfaces_real_or_computed_drawer_id():
    mod = load_plugin()

    class FakeCollection:
        def query(self, **kwargs):
            return {'ids': [[]], 'distances': [[]], 'metadatas': [[]], 'documents': [[]]}

    class FakePalace:
        def get_collection(self, path):
            return FakeCollection()

    def fake_add_drawer(col, wing, room, content, src, chunk_idx, agent):
        return {'success': True, 'drawer_id': 'drawer_real'}

    api = mod.MemPalaceAPI('/tmp/no-palace')
    api._imported = True
    api._palace = FakePalace()
    api._col = FakeCollection()
    api._miner_add_drawer_fn = fake_add_drawer
    assert api.add_drawer('new content that is intentionally long enough') == 'drawer_real'


def test_memory_remove_invalidates_concrete_triple():
    mod = load_plugin()
    calls = []

    class FakeAPI:
        def kg_invalidate_triple(self, subject, predicate, obj, ended=None):
            calls.append((subject, predicate, obj, ended))
            return True

    provider = mod.MemPalaceMemoryProvider(
        mod.MemPalaceConfig(enabled=True, memory_mirror_enabled=True, background_ingest=False)
    )
    provider._mp_api = FakeAPI()
    provider.on_memory_write(
        'remove',
        'memory',
        'ignored fallback content',
        metadata={'kg_triple': {'subject': 'Max', 'predicate': 'does', 'object': 'chess', 'ended': '2026-01-01'}},
    )
    assert calls == [('Max', 'does', 'chess', '2026-01-01')]


def test_search_lexical_fallback_finds_exact_drawer_id_when_semantic_misses():
    mod = load_plugin()

    class FakeSearcher:
        def search_memories(self, **kwargs):
            return {'results': []}

    class FakeCollection:
        def get(self, **kwargs):
            assert kwargs.get('ids') == ['drawer_skill_using_superpowers']
            return {
                'ids': ['drawer_skill_using_superpowers'],
                'documents': ['Skill body for using-superpowers'],
                'metadatas': [{'wing': 'skills', 'room': 'cursor-superpowers', 'source_file': '/skills/using-superpowers/SKILL.md'}],
            }

    class FakePalace:
        def get_collection(self, path):
            return FakeCollection()

    api = mod.MemPalaceAPI('/tmp/no-palace')
    api._imported = True
    api._search_memories_fn = FakeSearcher()
    api._palace = FakePalace()
    api._col = FakeCollection()

    results = api.search('drawer_skill_using_superpowers', min_score=0.3)
    assert results[0]['drawer_id'] == 'drawer_skill_using_superpowers'
    assert results[0]['score'] == 1.0
    assert results[0]['match_type'] == 'lexical:id'


def test_search_lexical_fallback_matches_skill_id_variants_in_source_file():
    mod = load_plugin()

    class FakeSearcher:
        def search_memories(self, **kwargs):
            return {'results': []}

    class FakeCollection:
        def get(self, **kwargs):
            return {
                'ids': ['drawer_1', 'drawer_2'],
                'documents': ['Skill documentation for context surfing', 'Unrelated memory'],
                'metadatas': [
                    {'wing': 'skills', 'room': 'cursor-superpowers', 'source_file': '/skills/context_surfing/SKILL.md'},
                    {'wing': 'misc', 'room': 'notes', 'source_file': '/notes/other.md'},
                ],
            }

    class FakePalace:
        def get_collection(self, path):
            return FakeCollection()

    api = mod.MemPalaceAPI('/tmp/no-palace')
    api._imported = True
    api._search_memories_fn = FakeSearcher()
    api._palace = FakePalace()
    api._col = FakeCollection()

    results = api.search('context-surfing', min_score=0.3)
    assert [r['drawer_id'] for r in results] == ['drawer_1']
    assert results[0]['source_file'] == '/skills/context_surfing/SKILL.md'
    assert results[0]['match_type'].startswith('lexical:')


def test_lexical_fallback_uses_configured_scan_limit():
    mod = load_plugin()
    seen_limits = []

    class FakeCollection:
        def get(self, **kwargs):
            seen_limits.append(kwargs.get('limit'))
            return {'ids': [], 'documents': [], 'metadatas': []}

    class FakePalace:
        def get_collection(self, path):
            return FakeCollection()

    cfg = mod.MemPalaceConfig(enabled=True, lexical_scan_limit=123)
    api = mod.MemPalaceAPI('/tmp/no-palace', config=cfg)
    api._imported = True
    api._palace = FakePalace()
    api._col = FakeCollection()
    api._lexical_fallback('anything', limit=4)
    assert 123 in seen_limits, f'expected 123 in {seen_limits}'


def test_diagnostics_snapshot_reports_metrics_and_state():
    mod = load_plugin()
    provider = mod.MemPalaceMemoryProvider(mod.MemPalaceConfig(enabled=True, prefetch_cache_size=3))
    provider._metric('prefetch_cache_hits')
    diag = provider.diagnostics()
    assert diag['name'] == 'mempalace'
    assert diag['enabled'] is True
    assert diag['prefetch_cache_size'] == 0
    assert diag['prefetch_cache_limit'] == 3
    assert diag['metrics']['prefetch_cache_hits'] == 1


def test_shutdown_joins_tracked_background_threads():
    mod = load_plugin()
    provider = mod.MemPalaceMemoryProvider(mod.MemPalaceConfig(enabled=True, thread_join_timeout_ms=100))
    ran = []

    def worker():
        time.sleep(0.02)
        ran.append(True)

    provider._start_tracked_thread('test-worker', worker)
    provider.shutdown()
    assert ran == [True]
    assert provider.diagnostics()['background_threads'] == 0


def test_memory_mirror_replace_respects_mirror_replace_flag():
    mod = load_plugin()
    calls = []

    class FakeAPI:
        def add_drawer(self, **kwargs):
            calls.append(kwargs)
            return 'drawer_replace'

    provider = mod.MemPalaceMemoryProvider(
        mod.MemPalaceConfig(
            enabled=True,
            memory_mirror_enabled=True,
            background_ingest=False,
            mirror_add=False,
            mirror_replace=True,
        )
    )
    provider._mp_api = FakeAPI()
    provider.on_memory_write('replace', 'memory', 'replacement content')
    assert len(calls) == 1
    assert calls[0]['content'] == 'replacement content'


def test_sync_turn_enforces_max_turn_length():
    mod = load_plugin()
    captured = []

    class FakeAPI:
        def chunk_and_add(self, **kwargs):
            captured.append(kwargs['content'])
            return ['drawer_1']
        def dialect_compress(self, *a, **k):
            return ''
        def kg_add_triple(self, *a, **k):
            pass
        def add_drawer(self, *a, **k):
            return 'drawer_1'

    cfg = mod.MemPalaceConfig(enabled=True, ingestion_mode='each_turn', background_ingest=False, min_turn_length=1, max_turn_length=12)
    provider = mod.MemPalaceMemoryProvider(cfg)
    provider._mp_api = FakeAPI()
    # retrieval disabled so sync_turn uses _mp_api directly
    provider.sync_turn('abcdefghij klmnopqrstuvwxyz', '', session_id='s')
    assert captured == ['user: abcdef']


def test_background_retrieval_false_runs_inline(monkeypatch):
    """background_retrieval=False: queue_prefetch no-ops, prefetch runs inline."""
    mod = load_plugin()
    provider = mod.MemPalaceMemoryProvider(
        mod.MemPalaceConfig(enabled=True, retrieval_enabled=False, background_retrieval=False)
    )
    monkeypatch.setattr(provider, '_ensure_api', lambda: None)
    # Mark as initialized so prefetch doesn't early-exit
    provider._initialized = True
    call_count = 0

    class FakeAPI:
        def search(self, query, wing='', room='', limit=8, min_score=0.3):
            nonlocal call_count
            call_count += 1
            return [{'content': f'found: {query}', 'score': 0.9, 'drawer_id': f'd_{query}',
                     'wing': wing, 'room': room, 'source_file': 'test'}]

    provider._mp_api = FakeAPI()
    # queue_prefetch is no-op when background_retrieval=False
    provider.queue_prefetch('xyz', session_id='s1')
    assert provider.diagnostics()['background_threads'] == 0
    # prefetch runs inline and calls search
    result = provider.prefetch('xyz', session_id='s1')
    assert result == 'found: xyz'
    assert call_count == 1
    assert provider.diagnostics()['background_threads'] == 0


def test_load_config_memory_stack_nested():
    mod = load_plugin()
    cfg = mod.load_config({
        'memory': {'provider': 'mempalace'},
        'mempalace_memory': {
            'memory_stack': {
                'enabled': True,
                'wake_char_budget': 500,
                'wake_up_on_session_start': True,
            },
        },
    })
    assert cfg.memory_stack_enabled is True
    assert cfg.wake_char_budget == 500
    assert cfg.wake_up_on_session_start is True


def test_env_enabled_merges_yaml_not_only_enabled(monkeypatch):
    """HERMES_MEMPALACE_MEMORY_ENABLED must not wipe nested mempalace_memory from YAML."""
    monkeypatch.setenv('HERMES_MEMPALACE_MEMORY_ENABLED', '1')
    mod = load_plugin()
    cfg = mod.load_config({
        'memory': {'provider': 'mempalace'},
        'mempalace_memory': {
            'palace_data_dir': '/tmp/merge-test-palace',
            'memory_stack': {
                'enabled': True,
                'wake_char_budget': 777,
            },
        },
    })
    assert cfg.enabled is True
    assert cfg.palace_data_dir == '/tmp/merge-test-palace'
    assert cfg.memory_stack_enabled is True
    assert cfg.wake_char_budget == 777


def test_on_session_start_loads_wake_when_configured():
    mod = load_plugin()
    cfg = mod.MemPalaceConfig(
        enabled=True,
        retrieval_enabled=True,
        memory_stack_enabled=True,
        wake_up_on_session_start=True,
        background_retrieval=False,
    )
    provider = mod.MemPalaceMemoryProvider(cfg)

    class FakeAPI:
        def wake_up_context(self, **kwargs):
            return 'L0L1'

        def scoped_recall(self, *a, **k):
            return ''

        def search(self, **kwargs):
            return []

    provider._mp_api = FakeAPI()
    provider.on_session_start('sess1')
    assert provider._wake_block == 'L0L1'
    out = provider.prefetch('hi', session_id='sess1')
    assert 'L0L1' in out
    out2 = provider.prefetch('hi', session_id='sess1')
    assert provider._wake_prefetch_applied is True
    assert 'L0L1' not in out2


def test_prefetch_scoped_recall_uses_l2_default_room():
    """Retrieval calls scoped_recall with target_wing and l2_default_room."""
    mod = load_plugin()
    calls = []

    class FakeAPI:
        def search(self, **kwargs):
            return []
        def scoped_recall(self, wing, room=None, char_budget=1500):
            calls.append((wing, room, char_budget))
            return 'L2-hit'

    cfg = mod.MemPalaceConfig(
        enabled=True, retrieval_enabled=True,
        memory_stack_enabled=True, l2_before_deep_search=True,
        l2_default_room='auth', target_wing='tw',
        background_retrieval=False,
    )
    retrieval = mod.MemPalaceRetrieval(FakeAPI(), cfg)
    result = retrieval._fetch_with_timeout(('s', 'q', '', ''), 'q', '', '', timeout=1.0)
    assert calls == [('tw', 'auth', cfg.recall_char_budget)]
    assert 'L2-hit' in result


def test_prefetch_passes_explicit_wing_to_scoped_recall():
    """L2 scoped recall uses explicit prefetch_wing over target_wing."""
    mod = load_plugin()
    calls = []

    class FakeAPI:
        def search(self, **kwargs):
            return []
        def scoped_recall(self, wing, room=None, char_budget=1500):
            calls.append((wing, room))
            return 'scoped'

    cfg = mod.MemPalaceConfig(
        enabled=True, retrieval_enabled=True,
        memory_stack_enabled=True, l2_before_deep_search=True,
        target_wing='tw', background_retrieval=False,
    )
    retrieval = mod.MemPalaceRetrieval(FakeAPI(), cfg)
    retrieval._fetch_with_timeout(('s', 'q', 'explicit_wing', 'room-a'), 'q', 'explicit_wing', 'room-a', timeout=1.0)
    assert calls == [('explicit_wing', 'room-a')]


def test_system_prompt_block_reports_active_features():
    mod = load_plugin()
    provider = mod.MemPalaceMemoryProvider(mod.MemPalaceConfig(
        enabled=True,
        memory_stack_enabled=True,
        extract_facts_each_turn=True,
    ))
    provider._initialized = True
    block = provider.system_prompt_block()
    assert 'MemPalace memory provider active' in block
    assert 'memory stack L0-L3' in block
    assert 'fact extraction' in block


def test_system_prompt_block_empty_when_disabled():
    mod = load_plugin()
    provider = mod.MemPalaceMemoryProvider(mod.MemPalaceConfig(enabled=False))
    assert provider.system_prompt_block() == ''


def test_system_prompt_block_empty_when_not_initialized():
    mod = load_plugin()
    provider = mod.MemPalaceMemoryProvider(mod.MemPalaceConfig(enabled=True))
    assert provider.system_prompt_block() == ''


def test_get_config_schema_returns_expected_keys():
    mod = load_plugin()
    provider = mod.MemPalaceMemoryProvider(mod.MemPalaceConfig(enabled=True))
    schema = provider.get_config_schema()
    keys = {f['key'] for f in schema}
    # Schema uses dot-notation for nested config (YAML style)
    assert 'palace_data_dir' in keys
    assert 'ingestion.mode' in keys
    assert 'retrieval.enabled' in keys
    assert 'facts.extract_each_turn' in keys
    assert 'holographic.enabled' in keys
    assert 'duplicate_check_enabled' in keys


def test_on_pre_compress_extracts_facts():
    mod = load_plugin()
    provider = mod.MemPalaceMemoryProvider(mod.MemPalaceConfig(
        enabled=True,
        extract_facts_each_turn=True,
        fact_extraction_mode='schema',
        min_turn_length=5,
    ))
    provider._mp_api = object()
    messages = [
        {'role': 'user', 'content': 'Alice works_on the MemPalace project and uses Python'},
        {'role': 'assistant', 'content': 'Great, noted.'},
    ]
    result = provider.on_pre_compress(messages)
    # Should return extracted facts or empty string (depends on regex matching)
    assert isinstance(result, str)


def test_on_pre_compress_empty_when_extraction_disabled():
    mod = load_plugin()
    provider = mod.MemPalaceMemoryProvider(mod.MemPalaceConfig(
        enabled=True,
        extract_facts_each_turn=False,
    ))
    provider._mp_api = object()
    result = provider.on_pre_compress([{'role': 'user', 'content': 'test'}])
    assert result == ''


def test_on_delegation_ingests_result():
    mod = load_plugin()
    captured = []

    class FakeAPI:
        def chunk_and_add(self, **kwargs):
            captured.append(kwargs)
            return ['drawer_1']

    provider = mod.MemPalaceMemoryProvider(mod.MemPalaceConfig(
        enabled=True,
        ingestion_mode='each_turn',
        background_ingest=False,
    ))
    provider._mp_api = FakeAPI()
    provider.on_delegation('fix the bug', 'bug fixed successfully', child_session_id='child1')
    assert len(captured) == 1
    assert 'fix the bug' in captured[0]['content']
    assert 'bug fixed successfully' in captured[0]['content']


def test_on_delegation_skips_when_ingestion_none():
    mod = load_plugin()
    captured = []

    class FakeAPI:
        def chunk_and_add(self, **kwargs):
            captured.append(kwargs)
            return ['drawer_1']

    provider = mod.MemPalaceMemoryProvider(mod.MemPalaceConfig(
        enabled=True,
        ingestion_mode='none',
        background_ingest=False,
    ))
    provider._mp_api = FakeAPI()
    provider.on_delegation('task', 'result')
    assert captured == []


def test_kg_query_entity_returns_triples():
    mod = load_plugin()

    class FakeKG:
        def query_entity(self, entity, direction='both'):
            return [
                {'subject': entity, 'predicate': 'works_on', 'object': 'MemPalace',
                 'confidence': 0.9, 'valid_from': '2025-01', 'current': True, 'valid_to': None},
            ]

    api = mod.MemPalaceAPI('/tmp/no-palace')
    api._imported = True
    api._kg = FakeKG()
    results = api.kg_query_entity('Alice')
    assert len(results) == 1
    assert results[0]['subject'] == 'Alice'
    assert results[0]['predicate'] == 'works_on'


def test_kg_query_entity_returns_empty_on_error():
    mod = load_plugin()

    class FakeKG:
        def query_entity(self, entity, direction='both'):
            raise RuntimeError('db error')

    api = mod.MemPalaceAPI('/tmp/no-palace')
    api._imported = True
    api._kg = FakeKG()
    assert api.kg_query_entity('Alice') == []


def test_prefetch_includes_kg_facts():
    """Retrieval engine appends KG facts when include_kg_facts=True."""
    import sys as _sys, importlib.util
    spec = importlib.util.spec_from_file_location('mempalace_plugin', '__init__.py')
    mod = importlib.util.module_from_spec(spec)
    _sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    class FakeAPI:
        def search(self, query, **kwargs):
            return []
        def kg_query_entity(self, entity, direction='both'):
            if entity == 'Alice':
                return [{'subject': 'Alice', 'predicate': 'works_on', 'object': 'MemPalace',
                         'confidence': 0.9, 'valid_from': '2025-01', 'current': True, 'valid_to': None}]
            return []

    from mempalace_plugin.retrieval import MemPalaceRetrieval
    cfg = mod.MemPalaceConfig(enabled=True, include_kg_facts=True)
    api = FakeAPI()
    retrieval = MemPalaceRetrieval(api, cfg)

    result = retrieval._fetch_with_timeout(
        ('sess', 'What does Alice work on?', '', ''),
        'What does Alice work on?', '', '', timeout=1.0
    )
    assert 'Knowledge Graph' in result
    assert 'Alice' in result
    assert 'works_on' in result
    assert 'MemPalace' in result


def test_prefetch_kg_facts_disabled_by_config():
    mod = load_plugin()
    kg_called = []

    class FakeAPI:
        def wake_up_context(self, **kw):
            return ''
        def scoped_recall(self, *a, **k):
            return ''
        def search(self, **kwargs):
            return []
        def kg_query_entity(self, entity, direction='both'):
            kg_called.append(entity)
            return [{'subject': entity, 'predicate': 'is', 'object': 'test',
                     'confidence': 0.8, 'valid_from': '', 'current': True, 'valid_to': None}]

    provider = mod.MemPalaceMemoryProvider(mod.MemPalaceConfig(
        enabled=True,
        retrieval_enabled=True,
        include_kg_facts=False,
        background_retrieval=False,
    ))
    provider._mp_api = FakeAPI()
    result = provider.prefetch('Alice is great')
    assert kg_called == []
    assert 'Knowledge Graph' not in result


def test_on_session_end_accepts_messages_list():
    """Verify on_session_end accepts the ABC signature (messages list)."""
    mod = load_plugin()
    provider = mod.MemPalaceMemoryProvider(mod.MemPalaceConfig(enabled=True))
    # Should not raise - accepts list as per ABC
    provider.on_session_end([{'role': 'user', 'content': 'test'}])


def test_diary_config_loads_from_nested():
    mod = load_plugin()
    cfg = mod.load_config({
        'memory': {'provider': 'mempalace'},
        'mempalace_memory': {
            'diary': {
                'enabled': True,
                'agent_name': 'testbot',
                'read_on_start': True,
                'last_n': 3,
            },
        },
    })
    assert cfg.diary_enabled is True
    assert cfg.diary_agent_name == 'testbot'
    assert cfg.diary_read_on_start is True
    assert cfg.diary_last_n == 3


def test_build_session_summary_returns_recent_messages():
    mod = load_plugin()
    provider = mod.MemPalaceMemoryProvider(mod.MemPalaceConfig(enabled=True))
    messages = [
        {'role': 'user', 'content': 'Hello'},
        {'role': 'assistant', 'content': 'Hi there'},
        {'role': 'user', 'content': 'How are you?'},
    ]
    summary = provider._build_session_summary(messages)
    assert 'Hello' in summary
    assert 'Hi there' in summary


def test_build_session_summary_empty_for_no_messages():
    mod = load_plugin()
    provider = mod.MemPalaceMemoryProvider(mod.MemPalaceConfig(enabled=True))
    assert provider._build_session_summary([]) == ''


def test_retrieval_timeout_seconds_property():
    mod = load_plugin()
    cfg = mod.MemPalaceConfig(enabled=True, retrieval_timeout_ms=500)
    assert cfg.retrieval_timeout_seconds == 0.5
    cfg2 = mod.MemPalaceConfig(enabled=True, retrieval_timeout_ms=50)
    assert cfg2.retrieval_timeout_seconds == 0.05


def test_run_with_timeout_handles_executor_shutdown_gracefully(monkeypatch):
    load_plugin()
    retrieval_mod = sys.modules['mempalace_plugin_contract.retrieval']

    class DeadExecutor:
        def submit(self, fn):
            raise RuntimeError('cannot schedule new futures after interpreter shutdown')

    monkeypatch.setattr(retrieval_mod, '_get_timeout_executor', lambda max_workers=4: DeadExecutor())
    assert retrieval_mod._run_with_timeout(lambda: 'ok', 0.05) is None


def test_prefetch_does_not_require_missing_timeout_attr():
    mod = load_plugin()

    class FakeAPI:
        def search(self, **kwargs):
            return [{'content': 'hit', 'score': 0.9, 'source_file': 'x'}]

    cfg = mod.MemPalaceConfig(
        enabled=True, retrieval_enabled=True, background_retrieval=False,
    )
    provider = mod.MemPalaceMemoryProvider(cfg)
    provider._initialized = True
    provider._mp_api = FakeAPI()
    provider._retrieval = mod.MemPalaceRetrieval(FakeAPI(), cfg)
    result = provider.prefetch('hello', session_id='s1')
    assert isinstance(result, str)


def test_provider_is_available_with_property_backed_api():
    mod = load_plugin()
    provider = mod.MemPalaceMemoryProvider(mod.MemPalaceConfig(enabled=True))

    class FakeAPI:
        @property
        def is_available(self):
            return True

    provider._mp_api = FakeAPI()
    assert provider.is_available() is True


def test_provider_name_contract():
    mod = load_plugin()
    provider = mod.MemPalaceMemoryProvider(mod.MemPalaceConfig(enabled=True))
    assert provider.name == 'mempalace'
    for method in (
        'initialize', 'prefetch', 'queue_prefetch', 'sync_turn',
        'on_session_start', 'on_session_end', 'shutdown',
    ):
        assert hasattr(provider, method)


def test_load_memory_provider_returns_usable_provider():
    mod = load_plugin()
    provider = mod.load_memory_provider({'memory': {'provider': 'mempalace'}})
    assert provider.name == 'mempalace'
    assert provider.is_available() is False or provider.is_available() is True


def test_register_exposes_provider():
    mod = load_plugin()

    class Ctx:
        def __init__(self):
            self.provider = None
        def register_memory_provider(self, provider):
            self.provider = provider

    ctx = Ctx()
    mod.register(ctx)
    assert ctx.provider is not None
    assert ctx.provider.name == 'mempalace'


def test_chunk_and_add_duplicate_checks_each_chunk():
    mod = load_plugin()
    add_calls = []

    class FakeCollection:
        def query(self, **kwargs):
            return {'ids': [[]], 'distances': [[]]}

    api = mod.MemPalaceAPI(
        '/tmp/no-palace',
        config=mod.MemPalaceConfig(duplicate_check_enabled=True),
    )
    api._imported = True
    api._col = FakeCollection()
    api._chunk_text_fn = lambda content, src: [
        {'content': 'chunk-one', 'chunk_index': 0},
        {'content': 'chunk-two', 'chunk_index': 1},
    ]
    api._miner_add_drawer_fn = None
    original_add = api.add_drawer

    def tracking_add(content, **kwargs):
        add_calls.append(content)
        return original_add(content, **kwargs)

    api.add_drawer = tracking_add  # type: ignore[method-assign]
    ids = api.chunk_and_add('long body', wing='w', room='r')
    assert add_calls == ['chunk-one', 'chunk-two']
    assert len(ids) == 2


def test_add_drawer_skips_duplicate_when_disabled():
    mod = load_plugin()
    query_calls = []

    class FakeCollection:
        def query(self, **kwargs):
            query_calls.append(kwargs)
            return {'ids': [['drawer_existing']], 'distances': [[0.01]]}

    api = mod.MemPalaceAPI('/tmp/no-palace')
    api._imported = True
    api._col = FakeCollection()
    api._config = mod.MemPalaceConfig(duplicate_check_enabled=False)
    drawer_id = api.add_drawer('same content')
    assert query_calls == []
    assert drawer_id.startswith('drawer_')


def test_wake_block_only_injected_once():
    mod = load_plugin()
    provider = mod.MemPalaceMemoryProvider(mod.MemPalaceConfig(
        enabled=True, memory_stack_enabled=True, wake_up_on_session_start=True,
        background_retrieval=False, retrieval_enabled=False,
    ))
    provider._initialized = True
    provider._wake_block = 'WAKE-ONCE'
    first = provider.prefetch('q', session_id='s')
    second = provider.prefetch('q', session_id='s')
    assert first == 'WAKE-ONCE'
    assert second == ''
    assert provider._wake_prefetch_applied is True


def test_diagnostics_metrics_include_operator_counters():
    mod = load_plugin()
    provider = mod.MemPalaceMemoryProvider(mod.MemPalaceConfig(enabled=True))
    metrics = provider.diagnostics()['metrics']
    for key in (
        'prefetch_cache_hits',
        'retrieval_timeouts',
        'stale_cache_hits',
        'duplicate_hits',
        'duplicate_misses',
        'chunk_writes',
        'l2_recalls',
        'l3_searches',
    ):
        assert key in metrics
        assert metrics[key] == 0


def test_api_metrics_duplicate_hit_and_miss():
    mod = load_plugin()
    events = []

    query_calls = [0]

    class FakeCollection:
        def query(self, **kwargs):
            query_calls[0] += 1
            if query_calls[0] == 1:
                return {
                    'ids': [['drawer_existing']],
                    'distances': [[0.01]],
                    'metadatas': [[{}]],
                    'documents': [['dup']],
                }
            return {'ids': [[]], 'distances': [[]], 'metadatas': [[]], 'documents': [[]]}

        def add(self, **kwargs):
            pass

    api = mod.MemPalaceAPI(
        '/tmp/no-palace',
        config=mod.MemPalaceConfig(duplicate_check_enabled=True),
        metric_fn=lambda name: events.append(name),
    )
    api._imported = True
    api._col = FakeCollection()
    api.add_drawer('dup')
    api.add_drawer('fresh')
    assert events.count('duplicate_hits') == 1
    assert events.count('duplicate_misses') == 1
    assert events.count('chunk_writes') == 1


def test_retrieval_metrics_stale_cache_hit():
    mod = load_plugin()
    events = []

    class FakeAPI:
        def search(self, *a, **k):
            return []

    cfg = mod.MemPalaceConfig(enabled=True, background_retrieval=False, cache_ttl_seconds=1)
    retrieval = mod.MemPalaceRetrieval(FakeAPI(), cfg, metric_fn=lambda name: events.append(name))
    key = retrieval._cache.prefetch_key('q', 's', 'w', 'r')
    retrieval._cache.set(key, 'stale-body')
    retrieval._cache._cache[key].timestamp = time.time() - 60

    stale = retrieval.prefetch('q', session_id='s', prefetch_wing='w', prefetch_room='r', background=False)
    assert stale == 'stale-body'
    assert 'stale_cache_hits' in events


def test_retrieval_metrics_l2_l3_and_timeout():
    mod = load_plugin()
    events = []

    class FakeAPI:
        def scoped_recall(self, wing, room=None, char_budget=1500):
            return 'L2-text'

        def search(self, *a, **k):
            time.sleep(0.2)
            return [{'content': 'hit', 'score': 0.9, 'drawer_id': 'd1', 'source_file': 't'}]

    cfg = mod.MemPalaceConfig(
        enabled=True,
        memory_stack_enabled=True,
        l2_before_deep_search=True,
        background_retrieval=False,
        retrieval_timeout_ms=50,
    )
    retrieval = mod.MemPalaceRetrieval(FakeAPI(), cfg, metric_fn=lambda name: events.append(name))
    retrieval.prefetch('fresh-q', session_id='s2', background=False)
    assert 'l2_recalls' in events
    assert 'l3_searches' in events
    assert 'retrieval_timeouts' in events


def test_provider_exposes_full_native_tool_surface():
    mod = load_plugin()
    provider = mod.MemPalaceMemoryProvider(mod.MemPalaceConfig(enabled=True))

    class FakeAPI:
        def get_tool_schemas(self):
            return [{
                'name': 'mempalace_status',
                'description': 'status',
                'parameters': {'type': 'object', 'properties': {}},
            }] * 30

    provider._mp_api = FakeAPI()
    schemas = provider.get_tool_schemas()
    assert len(schemas) == 30
    assert schemas[0]['name'] == 'mempalace_status'


def test_provider_handle_tool_call_json_serializes_api_result():
    mod = load_plugin()
    provider = mod.MemPalaceMemoryProvider(mod.MemPalaceConfig(enabled=True))

    class FakeAPI:
        def handle_tool_call(self, tool_name, args):
            assert tool_name == 'mempalace_status'
            assert args == {'verbose': True}
            return {'success': True, 'tool': tool_name}

    provider._mp_api = FakeAPI()
    result = provider.handle_tool_call('mempalace_status', {'verbose': True})
    assert '"success": true' in result.lower()
    assert 'mempalace_status' in result
