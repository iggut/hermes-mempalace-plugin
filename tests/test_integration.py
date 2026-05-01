"""Integration tests with real MemPalace installation.

These tests require a working MemPalace installation and palace directory.
They are SKIPPED by default — run with:

    MEMPALACE_INTEGRATION_TESTS=1 pytest tests/test_integration.py -v
"""

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

PLUGIN = Path('/home/iggut/.hermes/plugins/mempalace/__init__.py')

skip_integration = pytest.mark.skipif(
    not os.environ.get('MEMPALACE_INTEGRATION_TESTS'),
    reason='Integration tests disabled (set MEMPALACE_INTEGRATION_TESTS=1 to enable)',
)


def load_plugin():
    spec = importlib.util.spec_from_file_location('mempalace_plugin_integration', PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _has_mempalace_package():
    try:
        import mempalace  # noqa: F401
        return True
    except ImportError:
        return False


@skip_integration
@pytest.mark.skipif(not _has_mempalace_package(), reason='mempalace package not installed')
class TestMemPalaceIntegration:
    """Integration tests requiring a real MemPalace installation."""

    def test_api_import_succeeds(self):
        mod = load_plugin()
        api = mod.MemPalaceAPI(
            os.path.expanduser('~/.mempalace/palace'),
            mempalace_lib_dir=os.environ.get('MEMPALACE_LIB_DIR', ''),
        )
        api._ensure_imported()
        assert api._imported is True, f'Import failed: {api._import_error}'

    def test_search_returns_results(self):
        mod = load_plugin()
        api = mod.MemPalaceAPI(os.path.expanduser('~/.mempalace/palace'))
        api._ensure_imported()
        if not api._imported:
            pytest.skip('mempalace not importable')
        results = api.search('test query', limit=3, min_score=0.0)
        assert isinstance(results, list)

    def test_kg_add_and_query(self):
        mod = load_plugin()
        with tempfile.TemporaryDirectory() as td:
            api = mod.MemPalaceAPI(td)
            api._ensure_imported()
            if not api._imported:
                pytest.skip('mempalace not importable')
            # Add a triple
            ok = api.kg_add_triple('TestEntity', 'test_pred', 'TestObject', confidence=0.9)
            assert ok is True
            # Query it back
            results = api.kg_query_entity('TestEntity')
            assert len(results) >= 1
            assert any(r['predicate'] == 'test_pred' for r in results)

    def test_graph_traverse(self):
        mod = load_plugin()
        api = mod.MemPalaceAPI(os.path.expanduser('~/.mempalace/palace'))
        api._ensure_imported()
        if not api._imported:
            pytest.skip('mempalace not importable')
        results = api.graph_traverse('conversations', max_hops=1, limit=5)
        assert isinstance(results, list)

    def test_provider_is_available_with_real_palace(self):
        mod = load_plugin()
        palace_path = os.path.expanduser('~/.mempalace/palace')
        if not Path(palace_path).exists():
            pytest.skip('No palace at default path')
        cfg = mod.MemPalaceConfig(enabled=True, palace_data_dir=palace_path)
        provider = mod.MemPalaceMemoryProvider(cfg)
        assert provider.is_available() is True
