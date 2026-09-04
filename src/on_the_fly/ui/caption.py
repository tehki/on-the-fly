"""What the window shows, with no Qt in it.

The interesting decisions about a live translator's interface are not widget decisions, so
they live here where they can be tested without a display: what is on screen at each moment,
what replaces what, and what is deliberately not kept.

**There is no scrollback.** `session_caption_scrollback` is `EPHEMERAL` with
`enabled_by_default: false` and `default_retention_seconds: 0`, and
`docs/RETENTION_POLICY.md` states the consequence plainly: *live translation is fine,
scrollback is not*. A history pane would be the easiest feature in the application to build
and would quietly break the promise the whole project is arranged around, so this model
holds exactly one utterance. When the next one is finalised the previous is dropped, not
appended.

That is a real product cost and it should be described honestly rather than presented as a
feature: a user who looks away misses what was said. The alternative is a translator that
keeps a transcript of a private conversation, which is the thing ADR 0001 exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum


class Status(Enum):
    """What the application is doing, in the user's terms rather than the pipeline's."""

    IDLE = "idle"
    STARTING = "starting"
    LISTENING = "listening"
    STOPPING = "stopping"
    FAILED = "failed"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Caption:
    """One utterance on screen. Never a list of them.

    `source` is what the speaker said, updated live as the recogniser revises it.
    `translation` arrives only when the utterance is final — partials are not translated
    (ADR 0009), so it is `None` while someone is still talking.
    """

    source: str = ""
    translation: str | None = None
    is_final: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.source


@dataclass(frozen=True)
class ViewState:
    """Everything the window renders, in one immutable value."""

    status: Status = Status.IDLE
    caption: Caption = Caption()
    source_language: str = "en"
    target_language: str = "ru"
    detail: str = ""
    attribution: str = ""
    overflow_count: int = 0

    @property
    def can_start(self) -> bool:
        return self.status in (Status.IDLE, Status.FAILED)

    @property
    def can_stop(self) -> bool:
        return self.status in (Status.STARTING, Status.LISTENING)

    @property
    def is_busy(self) -> bool:
        return self.status in (Status.STARTING, Status.STOPPING)


class CaptionModel:
    """Holds the current view state and the rules for changing it.

    Every transition returns a new `ViewState`; nothing here mutates in place, so a widget
    layer can diff old against new and a test can assert on values rather than on a window.
    """

    def __init__(self, *, source_language: str = "en", target_language: str = "ru") -> None:
        self._state = ViewState(source_language=source_language, target_language=target_language)

    @property
    def state(self) -> ViewState:
        return self._state

    # -- lifecycle ---------------------------------------------------------------------

    def starting(self) -> ViewState:
        self._state = replace(
            self._state, status=Status.STARTING, detail="loading models", caption=Caption()
        )
        return self._state

    def listening(self, *, detail: str = "") -> ViewState:
        self._state = replace(self._state, status=Status.LISTENING, detail=detail)
        return self._state

    def stopping(self) -> ViewState:
        self._state = replace(self._state, status=Status.STOPPING, detail="")
        return self._state

    def stopped(self) -> ViewState:
        """Back to idle, and the caption goes with it.

        Leaving the last thing someone said on screen after they stopped listening is
        scrollback with one entry.
        """
        self._state = replace(
            self._state, status=Status.IDLE, detail="", caption=Caption(), overflow_count=0
        )
        return self._state

    def failed(self, reason: str) -> ViewState:
        """A failure clears the caption too: the text is stale and the reason matters more."""
        self._state = replace(self._state, status=Status.FAILED, detail=reason, caption=Caption())
        return self._state

    # -- captions ----------------------------------------------------------------------

    def partial(self, text: str) -> ViewState:
        """A revision of what is being said. Replaces whatever was there."""
        self._state = replace(
            self._state, caption=Caption(source=text, translation=None, is_final=False)
        )
        return self._state

    def final(self, text: str) -> ViewState:
        """The utterance settled. Its translation arrives separately and shortly."""
        self._state = replace(
            self._state, caption=Caption(source=text, translation=None, is_final=True)
        )
        return self._state

    def translated(self, translation: str) -> ViewState:
        """Attach a translation to the current final.

        Ignored when the caption is not final, because a translation can only belong to a
        finalised utterance, and attaching one to a partial would display a translation of
        text that is about to change.
        """
        if not self._state.caption.is_final:
            return self._state
        self._state = replace(
            self._state, caption=replace(self._state.caption, translation=translation)
        )
        return self._state

    # -- reporting ---------------------------------------------------------------------

    def note_overflow(self, count: int) -> ViewState:
        """Lost audio is a dropped word. The user is told, not protected from it."""
        self._state = replace(self._state, overflow_count=count)
        return self._state

    def set_languages(self, *, source: str, target: str) -> ViewState:
        self._state = replace(
            self._state, source_language=source, target_language=target, caption=Caption()
        )
        return self._state

    def set_attribution(self, attribution: str) -> ViewState:
        """CC-BY-4.0 obliges attribution a user can reach (ADR 0009)."""
        self._state = replace(self._state, attribution=attribution)
        return self._state
