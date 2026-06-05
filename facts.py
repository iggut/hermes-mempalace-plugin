"""Schema-validated fact extraction for MemPalace KG."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class FactSchema:
    """A structured fact with schema validation."""
    subject: str
    predicate: str
    object_: str
    confidence: float = 0.8
    valid_from: Optional[str] = None
    source: str = "auto_extracted"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "subject": self.subject,
            "predicate": self.predicate,
            "object_": self.object_,
            "confidence": self.confidence,
            "valid_from": self.valid_from,
            "source": self.source,
        }

    def validate(self) -> bool:
        if not self.subject or len(self.subject) < 2:
            return False
        if not self.predicate or len(self.predicate) < 1:
            return False
        if not self.object_ or len(self.object_) < 1:
            return False
        if not (0.0 <= self.confidence <= 1.0):
            return False
        return True


class SchemaValidatedFactExtractor:
    """Extract structured facts from conversation text.

    Uses entity detection + relationship pattern matching with schema
    validation. Designed to be conservative — low-confidence or
    malformed facts are discarded rather than stored.
    """

    # Relationship patterns
    DEFAULT_RELATIONSHIPS = [
        ("works_on", ["project", "system", "app", "repo"]),
        ("uses", ["tool", "library", "framework", "language"]),
        ("prefers", ["style", "format", "approach"]),
        ("has", ["account", "device", "key", "subscription"]),
        ("is", ["role", "title", "type"]),
        ("located_in", ["city", "timezone", "region"]),
        ("connected_to", ["network", "server", "device"]),
        ("started_on", ["date", "project"]),
        ("ended_on", ["date", "project"]),
    ]

    ENTITY_PATTERNS = [
        r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b',   # Multi-word proper nouns
        r'\b([A-Z]{2,}(?:\s+[A-Z]{2,})+)\b',        # ALL-CAPS acronyms
        r'\b([A-Z][a-z]{3,}(?:[0-9]+)?)\b',         # Single-word 4+ chars
    ]

    STOP_ENTITIES = {
        # Articles, pronouns, determiners
        "The", "This", "That", "These", "Those", "Each", "All", "No", "One", "Any",
        "Some", "Many", "Few", "Several", "Both", "Neither", "Either",
        # Question words
        "What", "Where", "When", "Which", "Who", "Whom", "Why", "How",
        # Common sentence-starting verbs
        "Let", "Make", "Take", "Give", "Get", "Set", "Put", "Run", "Use",
        "Try", "Keep", "Come", "Go", "See", "Look", "Find", "Show", "Work",
        "Need", "Want", "Ask", "Tell", "Say", "Said", "Think", "Know",
        # Sentence starters
        "Yes", "No", "Maybe", "Please", "Thanks", "Sure", "Well", "Now",
        "Here", "There", "Also", "Just", "Still", "Only", "Even", "Never",
        # Agent context
        "User", "Assistant", "System", "Session", "Turn", "Day", "Time",
        "Error", "Warning", "Info", "Debug", "Trace",
        # Common adjectives/adverbs
        "Good", "Bad", "New", "Old", "First", "Last", "Next", "Prev",
        "Big", "Small", "Long", "Short", "High", "Low", "Fast", "Slow",
        "Right", "Wrong", "True", "False", "Real", "Fake",
        "More", "Most", "Less", "Least", "Best", "Worst",
        # Technical terms
        "File", "Path", "Name", "Type", "Key", "Value", "List", "Dict",
        "String", "Int", "Float", "Bool", "Code", "Data", "Test",
        "Command", "Function", "Method", "Class", "Module", "Import",
        "Python", "Json", "Yaml", "Html", "Http", "Api", "Url",
        "Max", "Min", "Sum", "Avg", "Count", "Total", "Index",
        "None", "Null", "Default", "Config", "Option", "Setting",
    }

    @classmethod
    def extract_facts(
        cls,
        text: str,
        max_facts: int = 10,
        min_confidence: float = 0.7,
        mode: str = "schema",
        allowed_predicates: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Extract structured facts from text.

        Args:
            text: Conversation text
            max_facts: Maximum facts to return
            min_confidence: Minimum confidence threshold
            mode: "schema" (strict) or "regex" (lenient)
            allowed_predicates: Optional predicate allowlist

        Supported modes: "schema", "regex", "entity_detector".

        Returns:
            List of validated fact dicts
        """
        if not text or len(text) < 10:
            return []

        # entity_detector mode: delegate to the dedicated classmethod early
        if mode == "entity_detector":
            return cls.extract_facts_entity_detector(
                text, max_facts=max_facts, min_confidence=min_confidence,
            )

        entities = cls._find_entities(text)
        facts = []

        rels = (
            [(p, []) for p in allowed_predicates]
            if allowed_predicates
            else cls.DEFAULT_RELATIONSHIPS
        )

        for subj in entities:
            for pred, _ in rels:
                patterns = [
                    rf'\b{re.escape(subj)}\s+(?:is\s+)?{re.escape(pred)}\s+(\w+(?:\s+(?![.;,])\w+)*)',
                    rf"\b{re.escape(subj)}'s\s+{re.escape(pred)}\s+(\w+(?:\s+(?![.;,])\w+)*)",
                ]
                for pat in patterns:
                    match = re.search(pat, text)
                    if match:
                        obj_text = match.group(1).strip()
                        if len(obj_text) < 1 or len(obj_text) > 60:
                            continue
                        fact = FactSchema(
                            subject=subj,
                            predicate=pred,
                            object_=obj_text,
                            confidence=0.8,
                            source="auto_extracted",
                        )
                        if fact.validate() and fact.confidence >= min_confidence:
                            facts.append(fact.to_dict())
                        break

        # Fallback: noun-verb-noun for schema mode when no structured patterns matched
        if not facts and entities and mode == "schema":
            _FALLBACK_STOP_VERBS = {
                "is", "are", "was", "were", "be", "been", "being",
                "has", "have", "had", "do", "does", "did",
                "will", "would", "could", "should", "may", "might", "must",
                "can", "shall", "need", "want", "like", "know", "think",
                "said", "say", "tell", "told", "ask", "asked",
                "get", "got", "go", "went", "come", "came", "see", "saw",
                "make", "made", "take", "took", "give", "gave",
                "let", "put", "set", "run", "use", "used",
            }
            for subj in list(entities)[:3]:
                match = re.search(
                    rf'\b{re.escape(subj)}\s+(\w+)\s+(\w+(?:\s+\w+)*)',
                    text,
                )
                if match:
                    pred = match.group(1).lower()
                    obj_text = match.group(2).strip()
                    if pred in _FALLBACK_STOP_VERBS:
                        continue
                    if len(obj_text) < 2 or len(obj_text) > 60:
                        continue
                    if obj_text in cls.STOP_ENTITIES:
                        continue
                    fact = FactSchema(
                        subject=subj,
                        predicate=pred,
                        object_=obj_text,
                        confidence=0.65,
                        source="auto_extracted",
                    )
                    if fact.validate() and fact.confidence >= min_confidence:
                        facts.append(fact.to_dict())

        return facts[:max_facts]

    @classmethod
    def extract_facts_entity_detector(
        cls,
        text: str,
        max_facts: int = 10,
        min_confidence: float = 0.7,
        languages: tuple = ("en",),
    ) -> List[Dict[str, Any]]:
        """Extract facts using mempalace.entity_detector.detect_entities.

        Writes *text* to a temp file, passes it through the full
        entity-detection pipeline, and converts each detected entity
        into a FactSchema-compatible dict (``subject=entity_name,
        predicate='is_mentioned', object_=entity_type``).

        Returns an empty list if the mempalace package is not installed.
        """
        try:
            from mempalace.entity_detector import detect_entities  # type: ignore[import-untyped]
        except Exception:
            return []

        import tempfile, os  # noqa: E401

        facts: List[Dict[str, Any]] = []
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".txt")
            os.close(fd)
            with open(tmp_path, "w", encoding="utf-8") as fh:
                fh.write(text)

            detected = detect_entities([tmp_path], languages=languages)
            for category in ("people", "projects", "topics", "uncertain"):
                for ent in detected.get(category, []):
                    name = ent.get("name", "")
                    if not name or len(name) < 2:
                        continue
                    conf = float(ent.get("confidence", 0.5))
                    if conf < min_confidence:
                        continue
                    fact = FactSchema(
                        subject=name,
                        predicate="is_mentioned",
                        object_=ent.get("type", "entity") or "entity",
                        confidence=conf,
                        source="entity_detector",
                    )
                    if fact.validate():
                        facts.append(fact.to_dict())
        except Exception:
            pass
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        return facts[:max_facts]

    @classmethod
    def _find_entities(cls, text: str) -> List[str]:
        """Find capitalized entity names in text."""
        entities: List[str] = []
        seen = set()
        for pat in cls.ENTITY_PATTERNS:
            for match in re.finditer(pat, text):
                word = match.group(1)
                if word in cls.STOP_ENTITIES:
                    continue
                if word in seen:
                    continue
                seen.add(word)
                entities.append(word)
        return entities