import pytest

from labels import (
    FALLBACK_TAXONOMY, KIND_COLOR, KIND_GENERIC, KIND_MATERIAL, KIND_OBJECT, LabelTaxonomy,
    SearchLabel,
)

_ROWS = [
    {"label": "gadget", "canonical_term": None, "kind": KIND_GENERIC},
    {"label": "equipment", "canonical_term": None, "kind": KIND_GENERIC},
    {"label": "electronic device", "canonical_term": None, "kind": KIND_GENERIC},
    {"label": "plastic", "canonical_term": None, "kind": KIND_MATERIAL},
    {"label": "red", "canonical_term": None, "kind": KIND_COLOR},
    {"label": "road bicycle", "canonical_term": "bicycle", "kind": KIND_OBJECT},
    {"label": "bike", "canonical_term": "bicycle", "kind": KIND_OBJECT},
]
TAXONOMY = LabelTaxonomy.from_rows(_ROWS)


def _terms(labels, taxonomy=TAXONOMY):
    searchable, _ = taxonomy.searchable(labels)
    return [(s.term, s.vision_label) for s in searchable]


def test_unknown_label_is_an_object_searched_as_given():
    assert _terms(["Game controller"]) == [("Game controller", "Game controller")]


def test_material_and_colour_labels_are_skipped():
    searchable, skipped = TAXONOMY.searchable(["Plastic", "Red", "Wallet"])

    assert [s.term for s in searchable] == ["Wallet"]
    assert [(s.label, s.kind) for s in skipped] == [("Plastic", KIND_MATERIAL), ("Red", KIND_COLOR)]


def test_material_and_colour_only_match_the_whole_label():
    # "plastic bottle" and "red wagon" name objects, so they are still searched
    assert _terms(["Plastic bottle", "Red wagon"]) == [
        ("Plastic bottle", "Plastic bottle"), ("Red wagon", "Red wagon"),
    ]


def test_generic_words_match_inside_labels():
    searchable, skipped = TAXONOMY.searchable(["Sports equipment", "Bicycle"])

    assert [s.term for s in searchable] == ["Bicycle"]
    assert skipped[0].kind == KIND_GENERIC


def test_multi_word_generic_entry_matches_the_whole_label_only():
    assert _terms(["Electronic device", "Electronic keyboard"]) == [
        ("Electronic keyboard", "Electronic keyboard"),
    ]


def test_canonical_term_is_the_labels_term():
    assert _terms(["Road bicycle"]) == [("bicycle", "Road bicycle")]


def test_specific_label_is_queried_before_its_canonical_term():
    [search], _ = TAXONOMY.searchable(["Road bicycle"])

    assert search.queries == ("Road bicycle", "bicycle")


@pytest.mark.parametrize("search", [
    SearchLabel(term="Game controller", vision_label="Game controller"),  # unknown label
    SearchLabel(term="bicycle", vision_label=" Bicycle "),  # same term, other case/space
])
def test_label_that_is_its_own_canonical_term_is_queried_once(search):
    assert search.queries == (search.vision_label,)


def test_labels_with_the_same_canonical_term_are_searched_once():
    # The first (most confident) label wins; matching ignores case
    assert _terms(["Bicycle", "Road bicycle", "Bike", "Bicycle wheel"]) == [
        ("Bicycle", "Bicycle"), ("Bicycle wheel", "Bicycle wheel"),
    ]


def test_lookup_ignores_case_and_surrounding_space():
    assert _terms(["  ROAD Bicycle "]) == [("bicycle", "  ROAD Bicycle ")]


def test_unknown_label_is_searched_exactly_as_given():
    # Only the lookup is normalised; what reaches eBay (and its cache) is unchanged
    assert _terms(["  Canon Camera  "]) == [("  Canon Camera  ", "  Canon Camera  ")]


def test_blank_labels_are_ignored():
    assert _terms(["", "   ", "Lamp"]) == [("Lamp", "Lamp")]


@pytest.mark.parametrize("row", [
    {"label": "x", "canonical_term": None, "kind": "animal"},    # unknown kind
    {"label": "x", "canonical_term": None, "kind": KIND_OBJECT},  # object with no term
    {"label": "x", "canonical_term": "  ", "kind": KIND_OBJECT},  # object with a blank term
    {"label": "x", "canonical_term": "y", "kind": KIND_MATERIAL},  # term on a non-object
    {"label": " ", "canonical_term": None, "kind": KIND_GENERIC},  # blank label
    # rows of the wrong shape are invalid too, never a crash
    {"label": "x", "canonical_term": 5, "kind": KIND_OBJECT},      # term isn't text
    {"label": 42, "canonical_term": None, "kind": KIND_GENERIC},   # label isn't text
    {"label": "x", "kind": KIND_GENERIC},                          # column missing
])
def test_invalid_row_is_skipped_and_logged(row, caplog):
    taxonomy = LabelTaxonomy.from_rows([row, {"label": "plastic", "canonical_term": None,
                                               "kind": KIND_MATERIAL}])

    assert len(taxonomy) == 1  # the valid row is kept
    assert f"Label taxonomy has an invalid row: {row['label']}" in caplog.text


def test_taxonomy_with_no_valid_rows_is_empty():
    assert len(LabelTaxonomy.from_rows([{"label": "x", "canonical_term": None, "kind": "animal"}])) == 0


# The fallback reproduces the original hard-coded denylist exactly
@pytest.mark.parametrize("labels,expected", [
    (["gadget", "game controller"], ["game controller"]),
    (["Technology", "Electronic Device"], ["Electronic Device"]),
    (["sports equipment", "bicycle"], ["bicycle"]),
    (["gadget"], []),
    (["plastic", "wallet"], ["plastic", "wallet"]),  # no materials without the database
])
def test_fallback_taxonomy_is_the_original_denylist(labels, expected):
    assert [term for term, _ in _terms(labels, FALLBACK_TAXONOMY)] == expected
