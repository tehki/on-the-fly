"""What the window actually shows, rendered rather than reasoned about.

Every row in the window's warning area has been asserted through `ViewState` — the model says
`nothing_recognised`, and a test says the model says it. What nobody had checked is that the
widget then displays anything, or that the priority between the three rows is the one
`window.py` describes (ADR 0045's review trigger).

These build the real window under Qt's `offscreen` platform: no display is opened, no window
appears, and nothing here touches a microphone. They skip where PySide6 is not installed,
which is every CI run — `requirements-ui.txt` is an optional extra and the governance manifest
records that CI installs it nowhere.
"""

from __future__ import annotations

import os
from dataclasses import replace
from typing import Any

import pytest

from on_the_fly.domain.audio.levels import InputQuality
from on_the_fly.ui.caption import Caption, Status, ViewState

pytest.importorskip("PySide6", reason="the desktop extra is optional and CI installs it nowhere")

# Set before any Qt object exists: Qt reads it when the application is created, and a test
# run that opened a real window would put one on the screen of whoever ran the suite.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def application() -> object:
    from PySide6 import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def window(application: object) -> object:
    from on_the_fly.ui.window import build_window

    return build_window(
        languages=[("en", "English"), ("fr", "French")],
        targets_for=lambda code: [("ru", "Russian")],
        on_start=lambda source, target: None,
        on_stop=lambda: None,
    )


def listening(**overrides: Any) -> ViewState:
    """A state the window would be in mid-run, with one field changed per test."""
    state = ViewState(status=Status.LISTENING, source_language="en", target_language="ru")
    return replace(state, **overrides)


def test_a_caption_and_its_translation_reach_the_screen(window: object) -> None:
    window.apply_state(  # type: ignore[attr-defined]
        listening(caption=Caption(source="hello there", translation="привет", is_final=True))
    )

    assert window.source_label.text() == "hello there"  # type: ignore[attr-defined]
    assert window.translation_label.text() == "привет"  # type: ignore[attr-defined]


def test_attribution_is_displayed_because_the_licence_requires_it(window: object) -> None:
    """CC-BY-4.0 attribution that is computed and never shown is not attribution (ADR 0009)."""
    window.apply_state(listening(attribution="OPUS-MT, CC-BY-4.0"))  # type: ignore[attr-defined]

    assert "CC-BY-4.0" in window.attribution_label.text()  # type: ignore[attr-defined]


def test_nothing_recognised_reaches_the_warning_row(window: object) -> None:
    """The row added in ADR 0045, rendered for the first time here."""
    window.apply_state(listening(nothing_recognised=True))  # type: ignore[attr-defined]

    shown = window.warning_label.text()  # type: ignore[attr-defined]
    assert "nothing is being recognised" in shown
    assert "en" in shown, "the message must name the language that was asked for"


def test_a_bad_microphone_outranks_it(window: object) -> None:
    """Both explain an empty screen, and the input verdict is the one a user can act on:
    a model with nothing to say is a consequence of audio nobody could recognise."""
    window.apply_state(  # type: ignore[attr-defined]
        listening(nothing_recognised=True, input_quality=InputQuality.CLIPPING)
    )

    assert window.warning_label.text() == InputQuality.CLIPPING.advice  # type: ignore[attr-defined]


def test_dropped_audio_outranks_it_too(window: object) -> None:
    window.apply_state(listening(nothing_recognised=True, overflow_count=4))  # type: ignore[attr-defined]

    assert "4 audio block(s) dropped" in window.warning_label.text()  # type: ignore[attr-defined]


def test_a_clean_run_shows_no_warning_at_all(window: object) -> None:
    """Otherwise the row is furniture, and a user stops reading it."""
    window.apply_state(listening(caption=Caption(source="all fine")))  # type: ignore[attr-defined]

    assert window.warning_label.text() == ""  # type: ignore[attr-defined]


def test_the_warning_clears_when_the_finding_does(window: object) -> None:
    window.apply_state(listening(nothing_recognised=True))  # type: ignore[attr-defined]
    window.apply_state(listening(nothing_recognised=False))  # type: ignore[attr-defined]

    assert window.warning_label.text() == ""  # type: ignore[attr-defined]
