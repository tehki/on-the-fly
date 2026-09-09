"""Tests for the metrics the measurement scripts compute.

These functions produce numbers this project quotes as evidence: word error rates in
ADR 0031 and ADR 0035, chrF2 in ADR 0032 and ADR 0033, the room acoustics behind ADR 0030,
and every row of the sixteenth, seventeenth and eighteenth measurements. They had no tests
at all.

`chrf2` in particular is written out rather than taken from `sacrebleu`, which was a
deliberate choice — admitting a dependency to compute a number that fits in forty lines is
not a trade Article 12 would approve — and it was validated once, by hand, by reproducing
Helsinki-NLP's own published figure. A validation performed in a session that has since ended
is not a control. These are.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import measure_memory
import measure_room
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


# ---------------------------------------------------------------------------------------
# The budget's table governs the script that measures against it.
#
# `measure_latency.py` copies its thresholds out of the Targets table in
# `docs/PERFORMANCE_BUDGET.md`. A number duplicated out of a document drifts from it, and a
# tool measuring against a stale threshold reports the wrong verdict confidently.
# ---------------------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
BUDGET = REPO_ROOT / "docs" / "PERFORMANCE_BUDGET.md"


def millis(cell: str) -> float | None:
    """`"700 ms"` to `700.0`; anything else — an em dash, seconds, megabytes — to None."""
    if not cell.endswith(" ms"):
        return None
    try:
        return float(cell.removesuffix(" ms"))
    except ValueError:
        return None


def budget_targets() -> dict[str, tuple[float | None, float | None]]:
    """The `## Targets` table, as `metric -> (target ms, hard limit ms)`.

    Scoped to that one section. The document carries several other tables — the measurement
    history, the corrections — whose columns mean different things and whose cells happen to
    end in "ms" too, and reading those as targets is how a check ends up asserting against
    a number nobody set as a threshold.

    Rows not measured in milliseconds are skipped: the table also states seconds, megabytes
    and a percentage, and this script is about latency.
    """
    rows: dict[str, tuple[float | None, float | None]] = {}
    inside = False
    for line in BUDGET.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            inside = line.strip() == "## Targets"
            continue
        if not inside or not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 3 or cells[0] in ("Metric", "---"):
            continue
        target, hard = millis(cells[1]), millis(cells[2])
        if target is not None or hard is not None:
            rows[cells[0]] = (target, hard)
    return rows


def test_the_targets_table_is_read_and_carries_the_latency_rows() -> None:
    """Without this, a parser returning nothing would make every check below vacuous."""
    rows = budget_targets()

    assert "Endpoint → caption, p50" in rows
    assert "Endpoint → caption, p95" in rows
    assert "Endpoint → caption, p99" in rows


@pytest.mark.parametrize(
    ("metric", "constant"),
    [
        ("Endpoint → caption, p50", "TARGET_P50_MS"),
        ("Endpoint → caption, p95", "TARGET_P95_MS"),
    ],
)
def test_the_script_targets_match_the_document(metric: str, constant: str) -> None:
    import measure_latency

    assert getattr(measure_latency, constant) == budget_targets()[metric][0]


@pytest.mark.parametrize(
    ("metric", "constant"),
    [
        ("Endpoint → caption, p95", "HARD_LIMIT_P95_MS"),
        ("Endpoint → caption, p99", "HARD_LIMIT_P99_MS"),
    ],
)
def test_the_script_hard_limits_match_the_document(metric: str, constant: str) -> None:
    """p95's hard limit is the one that was missing.

    The budget's fourth measurement called 1476 ms "inside the p95 target, and only just";
    the fifth found a p95 of 2820 ms and recorded it as "past the 2500 ms hard limit". The
    number that made that a failure rather than a near miss was not in the tool that
    performs the method.
    """
    import measure_latency

    assert getattr(measure_latency, constant) == budget_targets()[metric][1]


def test_every_latency_threshold_in_the_table_is_one_the_script_knows() -> None:
    """The direction that catches a threshold being added to the document and nowhere else."""
    import measure_latency

    known = {
        measure_latency.TARGET_P50_MS,
        measure_latency.TARGET_P95_MS,
        measure_latency.HARD_LIMIT_P95_MS,
        measure_latency.HARD_LIMIT_P99_MS,
    }
    for metric, (target, hard) in budget_targets().items():
        if not metric.startswith("Endpoint → caption"):
            continue
        for value in (target, hard):
            assert value is None or value in known, (
                f"{metric} declares {value} ms, which measure_latency.py does not carry, "
                "so a run cannot report against it"
            )


@pytest.mark.parametrize(
    ("value", "target", "hard", "expected"),
    [
        (600.0, 700.0, None, "ok"),
        (700.0, 700.0, None, "ok"),
        (900.0, 700.0, None, "missed"),
        (1400.0, 1500.0, 2500.0, "ok"),
        (2000.0, 1500.0, 2500.0, "missed"),
        (2820.0, 1500.0, 2500.0, "PAST THE HARD LIMIT"),
        (3735.0, None, 4000.0, "ok"),
        (4200.0, None, 4000.0, "PAST THE HARD LIMIT"),
    ],
)
def test_the_verdict_says_which_threshold_was_crossed(
    value: float, target: float | None, hard: float | None, expected: str
) -> None:
    """ "Past the hard limit" outranks "missed": over the hard limit is also over the target,
    and reporting the weaker of the two would understate it. 2820 and 3735 are the figures
    the budget actually recorded."""
    import measure_latency

    assert measure_latency.verdict(value, target, hard).strip() == expected


# ---------------------------------------------------------------------------------------
# Room acoustics: the numbers behind ADR 0030.
#
# `measure_room.py` was the last measurement script no test reached, found by asking which
# first-party modules the suite never imports even transitively. Its three functions are
# pure signal processing with known answers, so they can be checked against arithmetic
# rather than against a recording of a room.
# ---------------------------------------------------------------------------------------


def decay(tau_seconds: float, *, seconds: float = 2.0) -> Any:
    """An impulse response that decays as `exp(-t/tau)` and nothing else."""
    import numpy as np

    return np.exp(-(np.arange(int(measure_room.RATE * seconds)) / measure_room.RATE) / tau_seconds)


@pytest.mark.parametrize("tau", [0.05, 0.1, 0.2, 0.4])
def test_reverberation_time_recovers_a_known_decay(tau: float) -> None:
    """RT60 has a closed form for a pure exponential, and this must return it.

    Amplitude `exp(-t/tau)` carries energy `exp(-2t/tau)`, so the Schroeder curve falls at
    `20/(tau ln 10)` dB per second and sixty decibels take `3 tau ln 10`. Nothing about that
    depends on a room, which is what makes it a test rather than a second opinion.
    """
    import numpy as np

    measured, _ = measure_room.reverberation_time(decay(tau))

    assert measured == pytest.approx(3 * tau * np.log(10), rel=1e-3)


def test_the_usable_decay_shrinks_as_the_room_slows() -> None:
    """The second return value is how much evidence the fit had, and it is not decoration.

    The fit window is fixed at 20-120 ms, so a slow room decays through less of it. A figure
    fitted over two decibels is far weaker than the same figure fitted over seventeen, and
    reporting the RT60 without it would hide that.
    """
    _, fast = measure_room.reverberation_time(decay(0.05))
    _, slow = measure_room.reverberation_time(decay(0.4))

    assert fast > slow
    assert fast == pytest.approx(17.4, abs=0.5)
    assert slow == pytest.approx(2.2, abs=0.5)


def test_the_fit_window_refuses_to_measure_the_noise_floor() -> None:
    """The defect the window exists for, reproduced.

    A comment in the script records a first attempt reporting 1.3 s for a response with 99%
    of its energy in the first 100 ms, because the Schroeder curve flattens at the
    measurement noise floor and a fit that includes the flat part reports the noise rather
    than the room. Here a 25 ms room on a floor 60 dB down reads 0.19 s through the window
    and 6.2 s without it.
    """
    import numpy as np

    rng = np.random.default_rng(0)
    room = decay(0.025)
    noisy = room + rng.normal(0.0, 10 ** (-60 / 20), room.shape)

    assert measure_room.early_energy_share(noisy, 100.0) > 0.99
    windowed, _ = measure_room.reverberation_time(noisy)
    assert windowed < 0.3

    # What a fit over the whole curve would have said instead.
    tail = noisy[int(np.argmax(np.abs(noisy))) :]
    energy = np.cumsum(tail[::-1] ** 2)[::-1]
    db = 10 * np.log10(np.maximum(energy / energy[0], 1e-12))
    slope = np.polyfit(np.arange(len(db)) / measure_room.RATE, db, 1)[0]

    assert -60.0 / slope > 5.0


def test_silence_reports_no_reverberation_time_rather_than_a_number() -> None:
    """No energy is not a fast room. `nan` refuses; a zero would be quoted."""
    import numpy as np

    measured, usable = measure_room.reverberation_time(np.zeros(measure_room.RATE))

    assert np.isnan(measured)
    assert usable == 0.0


def test_a_response_too_short_for_the_fit_window_reports_nothing() -> None:
    """The window ends at 120 ms; a recording shorter than that cannot be fitted."""
    import numpy as np

    measured, _ = measure_room.reverberation_time(decay(0.05, seconds=0.05))

    assert np.isnan(measured)


@pytest.mark.parametrize(
    ("window_ms", "expected"),
    [(50.0, 0.5), (150.0, 1.0)],
)
def test_early_energy_share_splits_two_taps_by_the_window(
    window_ms: float, expected: float
) -> None:
    """Two equal taps 100 ms apart: the first window holds one, the second holds both."""
    import numpy as np

    response = np.zeros(measure_room.RATE)
    response[1000] = 1.0
    response[1000 + measure_room.RATE // 10] = 1.0

    assert measure_room.early_energy_share(response, window_ms) == pytest.approx(expected)


def test_early_energy_share_is_measured_from_the_peak_not_the_start() -> None:
    """Silence before the direct sound is not part of the response.

    Counting from index zero would let the leading gap decide the answer, and the gap is an
    artefact of when the recording was armed.
    """
    import numpy as np

    impulse = np.zeros(measure_room.RATE)
    impulse[measure_room.RATE // 2] = 1.0

    assert measure_room.early_energy_share(impulse, 10.0) == pytest.approx(1.0)


def test_early_energy_share_of_silence_is_zero_rather_than_a_division_by_zero() -> None:
    import numpy as np

    assert measure_room.early_energy_share(np.zeros(measure_room.RATE), 50.0) == 0.0


def test_the_sweep_and_its_inverse_convolve_to_an_impulse() -> None:
    """The property the whole method rests on.

    An exponential sine sweep is useful because convolving it with its own inverse filter
    collapses to an impulse; the room's response is then whatever that convolution picks up
    instead. If this failed, every number the script reports would be measuring the sweep.
    """
    import numpy as np

    sweep, inverse = measure_room.sweep_and_inverse(1.0)
    convolved = np.convolve(sweep, inverse)
    energy = convolved**2
    peak = int(np.argmax(np.abs(convolved)))
    within_a_millisecond = measure_room.RATE // 1000

    assert peak == pytest.approx(len(convolved) // 2, abs=within_a_millisecond)
    share = energy[peak - within_a_millisecond : peak + within_a_millisecond].sum() / energy.sum()
    assert share > 0.98


def test_the_sweep_is_faded_in_and_out_and_stays_in_range() -> None:
    """A sweep that starts at full amplitude clicks, and a click is broadband energy the
    room responds to as much as the sweep does."""
    import numpy as np

    sweep, _ = measure_room.sweep_and_inverse(1.0)

    assert len(sweep) == measure_room.RATE
    assert sweep[0] == pytest.approx(0.0, abs=1e-9)
    assert sweep[-1] == pytest.approx(0.0, abs=1e-9)
    assert np.abs(sweep).max() <= 1.0


# --------------------------------------------------------------------------------------
# Resident memory. `docs/PERFORMANCE_BUDGET.md` has carried a 1200 MB target since it was
# written and nothing measured it until the twenty-seventh measurement, whose numbers come
# out of these two functions.
# --------------------------------------------------------------------------------------


def trace_of(*levels: float) -> list[tuple[float, float]]:
    return [(index * measure_memory.SAMPLE_INTERVAL, mb) for index, mb in enumerate(levels)]


def test_a_spike_is_not_a_level() -> None:
    """The distinction the whole measurement rests on. A single sample at 2 GB is an
    allocation on its way somewhere; what a run has to fit inside is what it holds."""
    held = 10 * [400.0]
    trace = trace_of(*[*held, 2000.0, *held])

    assert measure_memory.plateau(trace) == 400.0


def test_the_highest_sustained_level_wins_and_not_the_longest() -> None:
    """A run passes through several plateaus and the longest is usually the one where the
    least is loaded — before the models, or after teardown. The mode would return that."""
    trace = trace_of(*[*(100 * [200.0]), *(12 * [900.0]), *(100 * [200.0])])

    assert measure_memory.plateau(trace) == 900.0


def test_a_level_held_for_less_than_the_window_is_not_counted() -> None:
    trace = trace_of(*[*(20 * [300.0]), *(9 * [1500.0]), *(20 * [300.0])])

    assert measure_memory.plateau(trace) == 300.0


def test_a_trace_shorter_than_the_window_still_answers() -> None:
    """A run that exits in under half a second has no sustained level, and reporting zero for
    it would read as "used no memory" rather than "ended before this could tell"."""
    assert measure_memory.plateau(trace_of(100.0, 400.0, 250.0)) == 400.0
    assert measure_memory.plateau([]) == 0.0


def test_the_steps_are_where_something_was_loaded() -> None:
    trace = trace_of(100.0, 100.0, 480.0, 485.0, 900.0)
    found = measure_memory.steps(trace)

    assert len(found) == 2, found
    assert "+   380 MB" in found[0]
    assert "+   415 MB" in found[1]


def test_a_gradual_climb_is_not_a_step() -> None:
    """Otherwise every run reports a step, and the ones that mean "a model arrived" stop
    standing out."""
    assert measure_memory.steps(trace_of(*[100.0 + 5 * n for n in range(20)])) == []


@pytest.mark.parametrize(
    ("megabytes", "expected"),
    [(500.0, "inside"), (1199.0, "inside"), (1201.0, "over the 1200 MB target"), (2500.0, "hard")],
)
def test_the_verdict_names_the_line_that_was_crossed(megabytes: float, expected: str) -> None:
    assert expected in measure_memory.verdict(megabytes)


def test_reading_a_process_that_has_gone_is_not_an_error() -> None:
    """The process being measured is meant to exit, and doing so between the poll and the
    read is the expected race rather than a failure."""
    assert measure_memory.read_kb(2**22, "VmRSS:") is None
