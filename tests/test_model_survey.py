"""Tests for the survey of what publishers offer for the pairs this project does not serve.

`scripts/survey_translation_models.py` exists because three separate investigations in this
repository were somebody re-reading a decision and then checking whether the world still
matched it — and two of the three found the record stale, one of them four days old.

Nothing here touches the network. What is asserted is the parsing and the verdicts, which are
the parts that can be wrong in a way a live run would not reveal: a listing sorted the wrong
way names the wrong release as newest, and every conclusion drawn from it is about a model
nobody meant to look at.
"""

from __future__ import annotations

import urllib.request
from typing import Any

import pytest
import survey_translation_models as survey


def listing(prefix: str, *names: str) -> bytes:
    keys = "".join(f"<Key>{prefix}/{name}</Key>" for name in names)
    return f"<?xml version='1.0'?><ListBucketResult>{keys}</ListBucketResult>".encode()


# --------------------------------------------------------------------------------------
# Which directions get looked at
# --------------------------------------------------------------------------------------


def test_a_pinned_direction_is_not_surveyed() -> None:
    """The point is what is missing. A pair already pinned needs no publisher search."""
    pairs = survey.unserved_pairs()

    assert ("fr", "en") not in pairs
    assert ("en", "de") not in pairs


def test_every_supported_language_without_a_pin_is_surveyed() -> None:
    pairs = survey.unserved_pairs()

    assert ("es", "en") in pairs
    assert ("en", "es") in pairs
    assert ("en", "it") in pairs, "Italian is pinned one way only (ADR 0039)"
    assert ("it", "en") not in pairs


def test_english_is_never_surveyed_against_itself() -> None:
    assert not [pair for pair in survey.unserved_pairs() if pair[0] == pair[1]]


# --------------------------------------------------------------------------------------
# Reading a bucket listing
# --------------------------------------------------------------------------------------


def test_the_newest_release_is_the_latest_date_and_not_the_last_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`opus+bt-2021-04-30` sorts before `opus-2019-12-04` because `+` precedes `-`.

    Alphabetical ordering would call the 2019 release the newest one, and every judgement
    about tokenisers and licences after that would be about the wrong model.
    """
    monkeypatch.setattr(
        survey,
        "fetch",
        lambda url: listing("spa-eng", "opus-2019-12-04.zip", "opus+bt-2021-04-30.zip"),
    )

    assert survey.releases("bucket", "spa-eng")[-1] == "opus+bt-2021-04-30"


def test_a_release_carrying_two_dates_is_ordered_by_the_later_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`opusTCv20210807+bt_transformer-big_2022-03-13` names a corpus version first and its
    own release date second."""
    monkeypatch.setattr(
        survey,
        "fetch",
        lambda url: listing(
            "eng-spa",
            "opus-2021-02-19.zip",
            "opusTCv20210807+bt_transformer-big_2022-03-13.zip",
        ),
    )

    newest = survey.releases("bucket", "eng-spa")[-1]

    assert newest.endswith("2022-03-13")


def test_evaluation_archives_are_not_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        survey,
        "fetch",
        lambda url: listing("de-en", "opus-2020-02-26.zip", "opus-2020-02-26.eval.zip"),
    )

    assert survey.releases("bucket", "de-en") == ["opus-2020-02-26"]


def test_a_bucket_that_answers_nothing_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A prefix with no models is the normal answer for most language pairs."""
    monkeypatch.setattr(survey, "fetch", lambda url: None)

    assert survey.releases("bucket", "xx-yy") == []


# --------------------------------------------------------------------------------------
# Reading a manifest and a model card
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("pre-processing: normalization + SentencePiece (spm32k,spm32k)", "sentencepiece"),
        ("pre-processing: normalization + SentencePiece", "sentencepiece"),
        (
            "pre-processing: normalization + tokenization + BPE",
            "normalization + tokenization + BPE",
        ),
    ],
)
def test_the_tokeniser_is_read_from_the_publishers_own_manifest(
    monkeypatch: pytest.MonkeyPatch, line: str, expected: str
) -> None:
    """The BPE releases are the trap ADR 0032, ADR 0038 and ADR 0039 each hit in turn, and
    the manifest is where the publisher says so."""
    monkeypatch.setattr(survey, "fetch", lambda url: f"release: x\n{line}\n".encode())

    assert survey.tokeniser("bucket", "es-en", "opus-2019-12-04") == expected


def test_a_missing_manifest_says_so_rather_than_guessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(survey, "fetch", lambda url: None)

    assert survey.tokeniser("bucket", "es-en", "whatever") == "no manifest"


def test_the_original_weights_link_is_taken_from_the_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The link ADR 0033's chain depends on, and the one already found dead once."""
    card = (
        "* pre-processing: normalization + SentencePiece\n"
        "* download original weights: [opus-2020-08-18.zip]"
        "(https://object.pouta.csc.fi/Tatoeba-MT-models/spa-eng/opus-2020-08-18.zip)\n"
    )
    monkeypatch.setattr(survey, "fetch", lambda url: card.encode())

    named = survey.original_weights("Helsinki-NLP/opus-mt-es-en")

    assert named is not None
    assert named.endswith("spa-eng/opus-2020-08-18.zip")


def test_a_card_naming_no_weights_returns_nothing_rather_than_a_guess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(survey, "fetch", lambda url: b"# a model\n")

    assert survey.original_weights("Helsinki-NLP/opus-mt-xx-yy") is None


# --------------------------------------------------------------------------------------
# What an export has to declare
# --------------------------------------------------------------------------------------


def onnx_metadata(**overrides: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "cardData": {"license": "cc-by-4.0", "base_model": "Helsinki-NLP/opus-mt-es-en"},
        "siblings": [
            {"rfilename": "onnx/encoder_model_int8.onnx"},
            {"rfilename": "source.spm"},
            {"rfilename": "target.spm"},
        ],
    }
    metadata.update(overrides)
    return metadata


def verdict_for(monkeypatch: pytest.MonkeyPatch, metadata: dict[str, Any] | None) -> survey.Finding:
    monkeypatch.setattr(survey, "hugging_face", lambda repo: metadata)
    surveyed = survey.Survey(("es", "en"))
    survey.survey_onnx(surveyed, "es", "en")
    return surveyed.findings[-1]


def test_an_export_declaring_everything_is_usable(monkeypatch: pytest.MonkeyPatch) -> None:
    assert verdict_for(monkeypatch, onnx_metadata()).usable is True


def test_an_export_declaring_no_licence_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR 0018 refused `Xenova` on exactly this, and ADR 0039 refused an export inside the
    admitted organisation: admission is evidence about an artefact, not about a namespace."""
    finding = verdict_for(monkeypatch, onnx_metadata(cardData={"base_model": "x"}))

    assert finding.usable is False
    assert "declares no licence" in finding.detail


def test_an_export_with_no_sentencepiece_is_not_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This project's ONNX translator loads `source.spm`; an export of a BPE model has none."""
    finding = verdict_for(
        monkeypatch, onnx_metadata(siblings=[{"rfilename": "onnx/encoder_model_int8.onnx"}])
    )

    assert finding.usable is False


def test_no_export_at_all_is_reported_as_such(monkeypatch: pytest.MonkeyPatch) -> None:
    finding = verdict_for(monkeypatch, None)

    assert finding.usable is False
    assert "no export published" in finding.detail


# --------------------------------------------------------------------------------------
# Reaching nothing is not an answer
# --------------------------------------------------------------------------------------


def test_an_unreachable_url_is_none_rather_than_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Half this tool's job is asking about things that do not exist."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError("no network")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)

    assert survey.fetch("https://example.invalid/x") is None
    assert survey.exists("https://example.invalid/x") is False
