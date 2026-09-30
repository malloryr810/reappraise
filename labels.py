# Which Vision labels name something worth pricing, and what to search eBay for.
#
# The taxonomy is the `labels` table (migration 004): each row says whether a
# label is an object, a material, a colour, or a generic catch-all, and gives
# objects a canonical term so near-duplicates ("road bicycle", "bike") are
# searched once as "bicycle". Labels not in the table are treated as objects and
# searched as given; Vision emits far more labels than any table could list.
#
# Matching rules:
#   - an entry matches the whole label, ignoring case and surrounding space
#   - a single-word generic entry also matches as a whole word inside a label
#     ("equipment" catches "sports equipment"), as the original denylist did
#   - material and colour entries only ever match the whole label, so "plastic"
#     is skipped but "plastic bottle" and "red wagon" are still searched
#
# Searching: an object label is queried as Vision gave it first, since the
# specific label ("road bicycle") prices more precisely; its canonical term is
# the fallback query. The canonical term is also the category, the analytics key
# and the dedup key.
#
# FALLBACK_TAXONOMY reproduces the original hard-coded denylist, for when the
# database is unavailable. This module never touches the database itself.

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

KIND_OBJECT = "object"
KIND_MATERIAL = "material"
KIND_COLOR = "color"
KIND_GENERIC = "generic"
KINDS = frozenset({KIND_OBJECT, KIND_MATERIAL, KIND_COLOR, KIND_GENERIC})

logger = logging.getLogger("reappraise.labels")

_WORD = re.compile(r"[a-z]+")

# The generic-label denylist that lived in ebay.py before the taxonomy existed
_FALLBACK_GENERIC_WORDS = (
    "gadget", "technology", "product", "object", "item", "equipment", "supplies",
)


@dataclass(frozen=True)
class LabelEntry:
    kind: str
    # The term eBay is searched for; set for objects only
    canonical_term: str | None = None


@dataclass(frozen=True)
class SearchLabel:
    term: str          # canonical term: category, analytics and dedup key
    vision_label: str  # the Vision label it came from

    @property
    def queries(self) -> tuple[str, ...]:
        """eBay queries in order: the label as Vision gave it, then its canonical term."""
        if _key(self.vision_label) == _key(self.term):
            return (self.vision_label,)
        return (self.vision_label, self.term)


@dataclass(frozen=True)
class SkippedLabel:
    label: str
    kind: str


class LabelTaxonomy:
    def __init__(self, entries: Mapping[str, LabelEntry]) -> None:
        self._entries = MappingProxyType({_key(label): e for label, e in entries.items()})
        self._generic_words = frozenset(
            key for key, entry in self._entries.items()
            if entry.kind == KIND_GENERIC and " " not in key
        )

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, Any]]) -> "LabelTaxonomy":
        """Build from `labels` table rows, skipping (and logging) any invalid row.

        The table's ENUM and CHECK constraints enforce the same rules; they are
        checked again here because this is where the data enters the app. One
        bad row shouldn't cost the rest of the taxonomy, so it is skipped.
        """
        entries: dict[str, LabelEntry] = {}
        for row in rows:
            try:
                entries[row["label"]] = _validated_entry(row)
            except (ValueError, KeyError) as exc:
                logger.warning(
                    "Label taxonomy has an invalid row: %s (%s)", row.get("label"), exc
                )
        return cls(entries)

    def __len__(self) -> int:
        return len(self._entries)

    def classify(self, label: str) -> LabelEntry:
        key = _key(label)
        entry = self._entries.get(key)
        if entry is not None:
            return entry
        if not self._generic_words.isdisjoint(_WORD.findall(key)):
            return LabelEntry(KIND_GENERIC)
        return LabelEntry(KIND_OBJECT, label)  # searched exactly as Vision gave it

    def searchable(self, labels: Iterable[str]) -> tuple[list[SearchLabel], list[SkippedLabel]]:
        """Object labels to search, in the given order, and the labels skipped.

        Labels that share a canonical term are searched once, under the first
        (most confident) label. Only non-object labels are reported as skipped.
        """
        searchable: list[SearchLabel] = []
        skipped: list[SkippedLabel] = []
        seen_terms: set[str] = set()
        for label in labels:
            if not label.strip():
                continue
            entry = self.classify(label)
            if entry.kind != KIND_OBJECT:
                skipped.append(SkippedLabel(label, entry.kind))
            elif _key(entry.canonical_term) not in seen_terms:
                seen_terms.add(_key(entry.canonical_term))
                searchable.append(SearchLabel(entry.canonical_term, label))
        return searchable, skipped


def _key(label: str) -> str:
    return label.strip().lower()


def _validated_entry(row: Mapping[str, Any]) -> LabelEntry:
    label, kind, term = row["label"], row["kind"], row["canonical_term"]
    if not isinstance(label, str) or not isinstance(term, (str, type(None))):
        raise ValueError("label and canonical_term must be text")
    term = (term or "").strip()
    if not label.strip():
        raise ValueError("blank label")
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    if (kind == KIND_OBJECT) != bool(term):
        raise ValueError("only objects have a canonical term, and they must")
    return LabelEntry(kind, term or None)


FALLBACK_TAXONOMY = LabelTaxonomy(
    {word: LabelEntry(KIND_GENERIC) for word in _FALLBACK_GENERIC_WORDS}
)
