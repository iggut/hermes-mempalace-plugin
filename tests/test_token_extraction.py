"""Synthetic retrieval tests for token extraction and classification fixes.

These test the extraction and classification improvements from the Phase 4 edge-case
review pass: bare filenames, ports, tilde paths, absolute paths, and token-in-content
boundary checks.
"""
import pytest
from retrieval import _extract_query_tokens, _classify_evidence


class TestExtraction:
    """Verify token extraction handles real-world query patterns."""

    def test_bare_filename_cargo_toml(self):
        tokens = _extract_query_tokens("Cargo.toml is wrong")
        assert tokens["path"]  # should have cargo.toml in path or config

    def test_bare_filename_retrieval_py(self):
        tokens = _extract_query_tokens("fix retrieval.py")
        # retrieval.py should appear somewhere
        all_tokens = (tokens["path"] | tokens["config"] |
                      tokens["identifier"] | tokens["model"])
        flat = " ".join(all_tokens)
        assert "retrieval.py" in flat

    def test_port_8080_bare(self):
        tokens = _extract_query_tokens("port 8080 is failing")
        assert "8080" in tokens["port"]

    def test_port_host_port(self):
        tokens = _extract_query_tokens("127.0.0.1:8080")
        assert "8080" in tokens["port"]

    def test_port_localhost(self):
        tokens = _extract_query_tokens("localhost:3000")
        assert "3000" in tokens["port"]

    def test_tilde_path(self):
        tokens = _extract_query_tokens("~/Downloads/model.gguf")
        path_str = " ".join(tokens["path"])
        assert "downloads" in path_str.lower()

    def test_absolute_path(self):
        tokens = _extract_query_tokens("/home/iggut/.ssh/config")
        path_str = " ".join(tokens["path"])
        assert "ssh" in path_str.lower()

    def test_relative_path(self):
        tokens = _extract_query_tokens("open ./scripts/setup.sh")
        path_str = " ".join(tokens["path"])
        assert "scripts" in path_str

    def test_config_key(self):
        tokens = _extract_query_tokens("config key mempalace_memory.retrieval.max_recall_chars")
        assert any("mempalace_memory" in c for c in tokens["config"])

    def test_quoted_error(self):
        tokens = _extract_query_tokens("error \"messages field is required\"")
        assert any("messages field is required" in q for q in tokens["quoted"])


class TestClassification:
    """Verify evidence classification with lexical token matching."""

    def test_port_8080_match_is_strong(self):
        hit = {"content": "test service listens on port 8080", "score": 0.5}
        result = _classify_evidence("port 8080 is failing", hit)
        assert result == "strong"

    def test_different_port_is_not_strong(self):
        hit = {"content": "server listens on port 9090", "score": 0.5}
        result = _classify_evidence("port 8080 is failing", hit)
        # port 9090 ≠ port 8080 — high-specificity mismatch must not be strong
        assert result in ("medium", "weak"), f"Expected medium/weak, got {result}"

    def test_different_path_is_not_strong(self):
        hit = {"content": "fix provider.py", "score": 0.5}
        result = _classify_evidence("fix retrieval.py", hit)
        # retrieval.py ≠ provider.py — high-specificity mismatch must not be strong
        assert result in ("medium", "weak"), f"Expected medium/weak, got {result}"

    def test_path_mismatch_localhost_port(self):
        hit = {"content": "127.0.0.1:9090", "score": 0.5}
        result = _classify_evidence("127.0.0.1:8080", hit)
        # 9090 ≠ 8080 — port mismatch must not be strong
        assert result in ("medium", "weak"), f"Expected medium/weak, got {result}"

    def test_empty_query_is_not_strong(self):
        for content in ("anything here", "port 8080 server"):
            hit = {"content": content, "score": 0.9}
            result = _classify_evidence("", hit)
            assert result in ("medium", "weak"), f"Empty query: expected medium/weak, got {result}"

    def test_quoted_phrase_exact_match_is_strong(self):
        hit = {"content": "the error was: messages field is required", "score": 0.4}
        result = _classify_evidence('error "messages field is required"', hit)
        assert result == "strong", f"Expected strong for exact quoted match, got {result}"

    def test_quoted_phrase_mismatch_is_not_strong(self):
        hit = {"content": "a different error occurred", "score": 0.5}
        result = _classify_evidence('error "messages field is required"', hit)
        assert result in ("medium", "weak"), f"Expected medium/weak for mismatched quote, got {result}"

    def test_abs_path_match_is_strong(self):
        hit = {"content": "IdentityFile /home/iggut/.ssh/config", "score": 0.5}
        result = _classify_evidence("/home/iggut/.ssh/config", hit)
        assert result == "strong"

    def test_abs_path_mismatch_is_not_strong(self):
        # Different files in same directory: should NOT be strong
        hit = {"content": "IdentityFile /home/iggut/.ssh/id_ed25519", "score": 0.5}
        result = _classify_evidence("/home/iggut/.ssh/config", hit)
        assert result in ("medium", "weak"), f"Expected medium/weak, got {result}"

    def test_cargo_toml_match_is_strong(self):
        hit = {"content": "[package]\nname = \"rpgp\" in Cargo.toml", "score": 0.5}
        result = _classify_evidence("Cargo.toml is wrong", hit)
        assert result == "strong"

    def test_high_score_without_token_is_strong(self):
        hit = {"content": "unrelated content here", "score": 0.9}
        result = _classify_evidence("something else", hit)
        assert result == "strong"

    def test_low_score_without_token_is_weak(self):
        hit = {"content": "unrelated content here", "score": 0.3}
        result = _classify_evidence("something else", hit)
        assert result == "weak"

    def test_gguf_filename_is_extracted(self):
        tokens = _extract_query_tokens("model Carnice-Qwen3.6-MoE-35B-A3B-APEX-I-Compact.gguf")
        all_tokens = tokens["path"] | tokens["config"] | tokens["model"]
        flat = " ".join(all_tokens)
        assert "gguf" in flat


class TestRetrievalBlockCharacterBudget:
    """Verify recall blocks respect char budgets without full drawer dumps."""

    def test_classify_returns_weak_for_irrelevant(self):
        hit = {"content": "completely unrelated content about cooking", "score": 0.1}
        result = _classify_evidence("retrieval pipeline fix", hit)
        assert result == "weak"

    def test_near_duplicate_collapse(self):
        # Two hits from same source should each be classified independently
        hits = [
            {"content": "identityfile /home/iggut/.ssh/id_ed25519", "score": 0.95, "source": "ssh"},
            {"content": "identityfile /home/iggut/.ssh/id_ed25519 (same source)", "score": 0.90, "source": "ssh"},
        ]
        results = [_classify_evidence("/home/iggut/.ssh/config", h) for h in hits]
        assert all(r == "strong" for r in results)
