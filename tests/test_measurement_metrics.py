"""Tests for the metrics the measurement scripts compute.

These four functions produce numbers this project quotes as evidence: word error rates in
ADR 0031 and ADR 0035, chrF2 in ADR 0032 and ADR 0033, and every row of the sixteenth,
seventeenth and eighteenth measurements. They had no tests at all.

`chrf2` in particular is written out rather than taken from `sacrebleu`, which was a
deliberate choice — admitting a dependency to compute a number that fits in forty lines is
not a trade Article 12 would approve — and it was validated once, by hand, by reproducing
Helsinki-NLP's own published figure. A validation performed in a session that has since ended
is not a control. These are.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from measure_recognition import normalise, word_errors
from measure_translation import chrf2, read_test_file

# scripts/ is placed on the import path by pythonpath in pyproject.toml.


# --------------------------------------------------------------------------------------
# Word error rate
# --------------------------------------------------------------------------------------


def test_identical_transcripts_have_no_errors() -> None:
    words = normalise("after early nightfall the yellow lamps")

    assert word_errors(words, words) == 0


@pytest.mark.parametrize(
    ("reference", "hypothesis", "expected", "why"),
    [
        ("a b c", "a b c", 0, "identical"),
        ("a b c", "a x c", 1, "one substitution"),
        ("a b c", "a c", 1, "one deletion"),
        ("a b c", "a b c d", 1, "one insertion"),
        ("a b c", "x y z", 3, "every word wrong"),
        ("a b c", "", 3, "nothing recognised is three deletions, not zero errors"),
        ("", "a b c", 3, "words invented from nothing are insertions"),
    ],
)
def test_word_errors_counts_the_three_edit_kinds(
    reference: str, hypothesis: str, expected: int, why: str
) -> None:
    assert word_errors(reference.split(), hypothesis.split()) == expected, why


def test_a_hypothesis_longer_than_its_reference_can_exceed_100_percent() -> None:
    """Which is not a bug, and is why ADR 0035 could report 133% on one clip.

    A recogniser that returns a different, longer sentence costs a substitution for every
    reference word plus an insertion for each extra one.
    """
    errors = word_errors("son actionnaire majoritaire".split(), "sur une action est armee".split())

    assert errors > 3


# --------------------------------------------------------------------------------------
# Normalisation, and what it deliberately does not do
# --------------------------------------------------------------------------------------


def test_case_and_punctuation_are_folded_away() -> None:
    """These models emit no punctuation, so scoring it would measure the reference's style."""
    assert normalise("After early nightfall, the lamps!") == normalise(
        "AFTER EARLY NIGHTFALL THE LAMPS"
    )


def test_accents_are_part_of_the_word_and_are_kept() -> None:
    """Folding them would score `ACHEMENIDE` as correct for `ACHÉMÉNIDE`, which a French
    reader would call wrong."""
    assert normalise("ACHÉMÉNIDE") != normalise("ACHEMENIDE")


def test_an_apostrophe_is_kept_because_it_joins_a_word() -> None:
    assert normalise("L'HISTOIRE") == ["L'HISTOIRE"]


def test_orthography_is_not_normalised_and_that_inflates_small_samples() -> None:
    """The limitation ADR 0035 had to discount by hand: three of Whisper's four "errors" in
    66 words were a spelling convention and a compound split, not misrecognitions."""
    assert word_errors(normalise("DISHONOURED"), normalise("dishonored")) == 1
    assert word_errors(normalise("FOR EVER"), normalise("forever")) == 2


# --------------------------------------------------------------------------------------
# chrF2
# --------------------------------------------------------------------------------------


def test_a_perfect_translation_scores_one_hundred() -> None:
    text = ["the yellow lamps would light up here and there"]

    assert chrf2(text, text) == pytest.approx(100.0)


def test_nothing_in_common_scores_zero() -> None:
    """No shared character n-gram at any order, so precision and recall are both the epsilon
    that keeps the F-score defined rather than dividing by zero."""
    assert chrf2(["xxxxxxxxxx"], ["yyyyyyyyyy"]) == pytest.approx(0.0, abs=1e-9)


def test_whitespace_is_removed_before_n_grams_are_taken() -> None:
    """Which is what makes the metric survive a pre-tokenised reference.

    The `fr-en` test set writes `I 'm sure I 'll regret this .`; a metric that scored
    whitespace would penalise a model for detokenising correctly.
    """
    assert chrf2(["I'm sure"], ["I 'm sure"]) == pytest.approx(100.0)


def test_a_closer_translation_scores_higher() -> None:
    reference = ["the yellow lamps would light up here and there"]
    close = ["the yellow lamps would light up here and now"]
    distant = ["a completely different sentence about something else"]

    assert chrf2(close, reference) > chrf2(distant, reference)
    assert chrf2(close, reference) < 100.0


def test_it_is_a_corpus_metric_rather_than_an_average_of_sentences() -> None:
    """Counts are accumulated across segments, then scored once.

    A per-sentence average weights a three-word sentence like a thirty-word one, and is a
    different metric with the same name. This pins which one is implemented: the corpus score
    of a long perfect segment with a short wrong one is not the mean of 100 and 0.
    """
    hypotheses = ["the yellow lamps would light up here and there", "xxxxxxxx"]
    references = ["the yellow lamps would light up here and there", "yyyyyyyy"]

    corpus = chrf2(hypotheses, references)
    sentence_average = (
        chrf2(hypotheses[:1], references[:1]) + chrf2(hypotheses[1:], references[1:])
    ) / 2

    assert corpus > sentence_average


def test_mismatched_lengths_are_refused_rather_than_zipped_short() -> None:
    """Silently truncating would score a run that lost sentences as though it had not."""
    with pytest.raises(ValueError, match="same length"):
        chrf2(["one", "two"], ["one"])


# --------------------------------------------------------------------------------------
# The publisher test-set format
# --------------------------------------------------------------------------------------


def test_a_publisher_test_file_reads_as_source_reference_hypothesis(tmp_path: Path) -> None:
    path = tmp_path / "test.txt"
    path.write_text(
        "Have you ever been to Switzerland?\n"
        "Ты уже бывал в Швейцарии?\n"
        "Вы когда-нибудь были в Швейцарии?\n"
        "\n"
        "She learned quickly.\n"
        "Она быстро училась.\n"
        "Она быстро научилась.\n",
        encoding="utf-8",
    )

    records = read_test_file(path, limit=None)

    assert len(records) == 2
    assert records[0][0] == "Have you ever been to Switzerland?"
    assert records[0][1] == "Ты уже бывал в Швейцарии?"
    assert records[0][2] == "Вы когда-нибудь были в Швейцарии?"


def test_a_limit_takes_a_prefix(tmp_path: Path) -> None:
    """And the prefix is not representative — the publisher's own output scores 65.58 over
    the first 300 sentences of the `en-ru` set and 66.95 over all 5000, on identical text.
    Two numbers are comparable only from the same slice."""
    path = tmp_path / "test.txt"
    path.write_text("".join(f"s{i}\nr{i}\nh{i}\n\n" for i in range(10)), encoding="utf-8")

    assert len(read_test_file(path, limit=3)) == 3
    assert len(read_test_file(path, limit=None)) == 10


def test_a_malformed_record_is_refused_rather_than_skipped(tmp_path: Path) -> None:
    """A silently shortened test set would change every number derived from it."""
    path = tmp_path / "test.txt"
    path.write_text("source\nreference\n\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="expected source / reference"):
        read_test_file(path, limit=None)


def test_an_empty_test_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "test.txt"
    path.write_text("\n\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="no records"):
        read_test_file(path, limit=None)
