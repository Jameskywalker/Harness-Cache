from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher

from .models import (
    FreshnessResult,
    Pointer,
    SourceRange,
    hash_text,
    utcnow,
)


@dataclass
class SourceDocument:
    source_id: str
    path: str
    text: str
    version: str
    modified_at: datetime

    @property
    def lines(self) -> list[str]:
        return self.text.splitlines()


class SourceRepository:
    def __init__(self) -> None:
        self._sources: dict[tuple[str, str], SourceDocument] = {}

    def add(self, source_id: str, path: str, text: str, version: str) -> SourceDocument:
        document = SourceDocument(source_id, path, text, version, utcnow())
        self._sources[(source_id, path)] = document
        return document

    def get(self, source_id: str, path: str) -> SourceDocument | None:
        return self._sources.get((source_id, path))

    def open_range(self, pointer: Pointer) -> SourceRange:
        document = self.get(pointer.source_id, pointer.path)
        if document is None:
            raise FileNotFoundError(f"unknown source {pointer.source_id}:{pointer.path}")
        lines = document.lines
        start = max(1, pointer.line_start)
        end = min(len(lines), pointer.line_end)
        selected = lines[start - 1 : end]
        text = "\n".join(selected)
        anchors_found = tuple(anchor for anchor in pointer.anchors if anchor in text)
        return SourceRange(
            source_id=pointer.source_id,
            path=pointer.path,
            source_version=document.version,
            line_start=start,
            line_end=end,
            text=text,
            range_hash=hash_text(text),
            anchors_found=anchors_found,
            fresh=document.version == pointer.source_version and hash_text(text) == pointer.range_hash,
        )

    def hydrate_pointer_hash(self, pointer: Pointer) -> Pointer:
        if pointer.range_hash:
            return pointer
        source_range = self.open_range(pointer)
        return pointer.copy(range_hash=source_range.range_hash)

    def verify(self, pointer: Pointer) -> FreshnessResult:
        document = self.get(pointer.source_id, pointer.path)
        if document is None:
            return FreshnessResult(False, False, False, False, 0.0, 1.0, "source not found")
        source_range = self.open_range(pointer)
        source_version_matches = document.version == pointer.source_version
        range_hash_matches = (not pointer.range_hash) or source_range.range_hash == pointer.range_hash
        anchor_confidence = self.anchor_confidence(pointer, source_range.text)
        drift_score = 1.0 - anchor_confidence
        if not source_version_matches:
            reason = "source version changed"
        elif not range_hash_matches:
            reason = "range hash mismatch"
        elif anchor_confidence < 0.75:
            reason = "anchor confidence below threshold"
        else:
            reason = "fresh"
        return FreshnessResult(
            fresh=source_version_matches and range_hash_matches and anchor_confidence >= 0.75,
            source_exists=True,
            source_version_matches=source_version_matches,
            range_hash_matches=range_hash_matches,
            anchor_confidence=anchor_confidence,
            drift_score=drift_score,
            reason=reason,
        )

    @staticmethod
    def anchor_confidence(pointer: Pointer, text: str) -> float:
        if not pointer.anchors:
            return 1.0
        matches = sum(1 for anchor in pointer.anchors if anchor in text)
        return matches / len(pointer.anchors)

    def reanchor(self, pointer: Pointer, min_anchor_confidence: float = 0.75) -> Pointer | None:
        document = self.get(pointer.source_id, pointer.path)
        if document is None:
            return None
        lines = document.lines
        span = max(1, pointer.line_end - pointer.line_start + 1)

        anchor_match = self._find_anchor_window(pointer, lines, span, min_anchor_confidence)
        if anchor_match is not None:
            line_start, line_end, confidence = anchor_match
            text = "\n".join(lines[line_start - 1 : line_end])
            return pointer.copy(
                line_start=line_start,
                line_end=line_end,
                source_version=document.version,
                range_hash=hash_text(text),
                anchor_confidence=confidence,
                drift_score=1.0 - confidence,
                reanchor_attempts=pointer.reanchor_attempts + 1,
                last_reanchor_result="success",
            )

        range_match = self._find_hash_window(pointer, lines, span)
        if range_match is not None:
            line_start, line_end = range_match
            return pointer.copy(
                line_start=line_start,
                line_end=line_end,
                source_version=document.version,
                anchor_confidence=0.8,
                drift_score=0.2,
                reanchor_attempts=pointer.reanchor_attempts + 1,
                last_reanchor_result="success",
            )

        fuzzy_match = self._find_fuzzy_window(pointer, lines, span)
        if fuzzy_match is not None:
            line_start, line_end, confidence = fuzzy_match
            text = "\n".join(lines[line_start - 1 : line_end])
            return pointer.copy(
                line_start=line_start,
                line_end=line_end,
                source_version=document.version,
                range_hash=hash_text(text),
                anchor_confidence=confidence,
                drift_score=1.0 - confidence,
                reanchor_attempts=pointer.reanchor_attempts + 1,
                last_reanchor_result="success",
            )

        return pointer.copy(
            anchor_confidence=0.0,
            drift_score=1.0,
            reanchor_attempts=pointer.reanchor_attempts + 1,
            last_reanchor_result="failed",
        )

    def _find_anchor_window(
        self,
        pointer: Pointer,
        lines: list[str],
        span: int,
        min_anchor_confidence: float,
    ) -> tuple[int, int, float] | None:
        if not pointer.anchors:
            return None
        for index in range(len(lines)):
            window = "\n".join(lines[index : min(len(lines), index + span)])
            confidence = self.anchor_confidence(pointer, window)
            if confidence >= min_anchor_confidence:
                return index + 1, min(len(lines), index + span), confidence
        return None

    def _find_hash_window(
        self,
        pointer: Pointer,
        lines: list[str],
        span: int,
    ) -> tuple[int, int] | None:
        if not pointer.range_hash:
            return None
        for index in range(max(0, len(lines) - span + 1)):
            window = "\n".join(lines[index : index + span])
            if hash_text(window) == pointer.range_hash:
                return index + 1, index + span
        return None

    def _find_fuzzy_window(
        self,
        pointer: Pointer,
        lines: list[str],
        span: int,
    ) -> tuple[int, int, float] | None:
        signals = " ".join((*pointer.anchors, *pointer.tags, pointer.title)).strip().lower()
        if not signals:
            return None
        best: tuple[int, int, float] | None = None
        for index in range(max(1, len(lines) - span + 1)):
            window = "\n".join(lines[index : index + span]).lower()
            score = SequenceMatcher(None, signals, window).ratio()
            if best is None or score > best[2]:
                best = (index + 1, min(len(lines), index + span), score)
        if best is not None and best[2] >= 0.35:
            return best
        return None
