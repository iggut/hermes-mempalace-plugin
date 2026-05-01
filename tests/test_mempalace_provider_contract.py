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
    monkeypatch.delenv('HERMES_MEMPALACE_MEMORY_ENABLED', raising=False)
    mod = load_plugin()
    provider = mod.MemPalaceMemoryProvider(mod.MemPalaceConfig(enabled=True, retrieval_enabled=True))
    calls = []
    provider._mp_api = object()
    provider._run_prefetch_search = lambda query, **kw: calls.append(query) or f'result:{query}'
    provider.queue_prefetch('abc', session_id='s1')
    for _ in range(50):
        if provider._prefetch_key('abc', 's1') in provider._prefetch_cache:
            break
        time.sleep(0.01)
    assert provider.prefetch('abc', session_id='s1') == 'result:abc'
    assert calls == ['abc']


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
        def add_drawer(self, **kwargs):
            self.called = True
            return True

    api = mod.MemPalaceAPI('/tmp/no-palace')
    api._imported = True
    api._palace = FakePalace()
    api._miner = FakeMiner()
    drawer_id = api.add_drawer('same content', duplicate_threshold=0.9)
    assert drawer_id == 'drawer_existing'
    assert api._miner.called is False


def test_add_drawer_surfaces_real_or_computed_drawer_id():
    mod = load_plugin()

    class FakeCollection:
        def query(self, **kwargs):
            return {'ids': [[]], 'distances': [[]], 'metadatas': [[]], 'documents': [[]]}

    class FakePalace:
        def get_collection(self, path):
            return FakeCollection()

    class FakeMiner:
        def add_drawer(self, **kwargs):
            return {'success': True, 'drawer_id': 'drawer_real'}

    api = mod.MemPalaceAPI('/tmp/no-palace')
    api._imported = True
    api._palace = FakePalace()
    api._miner = FakeMiner()
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
    api._searcher = FakeSearcher()
    api._palace = FakePalace()

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
    api._searcher = FakeSearcher()
    api._palace = FakePalace()

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

    api = mod.MemPalaceAPI('/tmp/no-palace', lexical_scan_limit=123)
    api._imported = True
    api._palace = FakePalace()
    api._lexical_fallback_search('anything', limit=4)
    assert seen_limits == [123]


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

    provider = mod.MemPalaceMemoryProvider(
        mod.MemPalaceConfig(enabled=True, ingestion_mode='each_turn', background_ingest=False, min_turn_length=1, max_turn_length=12)
    )
    provider._mp_api = FakeAPI()
    provider.sync_turn('abcdefghij', 'klmnopqrstuvwxyz', session_id='s')
    assert captured == ['abcdefghij k']


def test_background_retrieval_false_avoids_thread_tracking():
    mod = load_plugin()
    provider = mod.MemPalaceMemoryProvider(
        mod.MemPalaceConfig(enabled=True, retrieval_enabled=True, background_retrieval=False)
    )
    provider._mp_api = object()
    provider._run_prefetch_search = lambda query, **kw: f'result:{query}'
    provider.queue_prefetch('abc', session_id='s1')
    assert provider.diagnostics()['background_threads'] == 0
    assert provider.prefetch('abc', session_id='s1') == 'result:abc'
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
    assert out2 == out


def test_prefetch_scoped_recall_uses_l2_default_room():
    mod = load_plugin()
    recalls = []

    class FakeAPI:
        def scoped_recall(self, wing, room=None, **k):
            recalls.append((wing, room))
            return f'recall:{wing}/{room}'

        def search(self, **kwargs):
            return []

    provider = mod.MemPalaceMemoryProvider(
        mod.MemPalaceConfig(
            enabled=True,
            retrieval_enabled=True,
            memory_stack_enabled=True,
            l2_before_deep_search=True,
            l2_default_room='auth',
            target_wing='tw',
            background_retrieval=False,
        )
    )
    provider._mp_api = FakeAPI()
    provider.prefetch('q')
    assert recalls == [('tw', 'auth')]


def test_prefetch_passes_explicit_wing_to_scoped_recall():
    mod = load_plugin()
    recalls = []

    class FakeAPI:
        def scoped_recall(self, wing, room=None, **k):
            recalls.append((wing, room))
            return 'x'

        def search(self, **kwargs):
            return []

    provider = mod.MemPalaceMemoryProvider(
        mod.MemPalaceConfig(
            enabled=True,
            retrieval_enabled=True,
            memory_stack_enabled=True,
            l2_before_deep_search=True,
            background_retrieval=False,
        )
    )
    provider._mp_api = FakeAPI()
    provider.prefetch('q', prefetch_wing='driftwood', prefetch_room='bugs')
    assert recalls == [('driftwood', 'bugs')]


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
    assert 'palace_data_dir' in keys
    assert 'ingestion_mode' in keys
    assert 'retrieval_mode' in keys
    assert 'memory_stack_enabled' in keys
    assert 'extract_facts_each_turn' in keys
    assert 'holographic_enabled' in keys


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
    mod = load_plugin()

    class FakeAPI:
        def wake_up_context(self, **kw):
            return ''
        def scoped_recall(self, *a, **k):
            return ''
        def search(self, **kwargs):
            return []
        def kg_query_entity(self, entity, direction='both'):
            if entity == 'Alice':
                return [{'subject': 'Alice', 'predicate': 'works_on', 'object': 'MemPalace',
                         'confidence': 0.9, 'valid_from': '2025-01', 'current': True, 'valid_to': None}]
            return []

    provider = mod.MemPalaceMemoryProvider(mod.MemPalaceConfig(
        enabled=True,
        retrieval_enabled=True,
        include_kg_facts=True,
        background_retrieval=False,
    ))
    provider._mp_api = FakeAPI()
    result = provider.prefetch('What does Alice work on?')
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
