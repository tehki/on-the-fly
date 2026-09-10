"""Wiring the window to the pipeline (ADR 0016).

The composition root for the desktop application, in the same sense `app/cli.py` is one for
the command line: the only place that knows both a microphone and a translator exist.

**The pipeline runs on a worker thread.** Recognition and translation both block, and a
blocked Qt event loop is a frozen window — the single most common way a desktop application
of this shape is bad. The worker emits signals; Qt marshals them to the UI thread; the
window renders a `ViewState` and nothing else.

**Audio never reaches this file.** The worker consumes frames inside the pipeline and emits
text. Nothing here holds a buffer, and the retention store the pipeline builds purges on
exit exactly as it does for the CLI.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from on_the_fly.app.catalogue import recognisable_languages, translation_targets
from on_the_fly.domain.audio.levels import LevelWatchingSource
from on_the_fly.domain.audio.settling import SettlingSource
from on_the_fly.ui.caption import (
    NO_TRANSLATION,
    NO_TRANSLATION_LABEL,
    SPEECH_WITHOUT_TEXT_IS_A_FINDING_SECONDS,
    CaptionModel,
)

if TYPE_CHECKING:  # pragma: no cover - import shape only
    from collections.abc import Sequence

    from on_the_fly.domain.audio.ports import AudioSource

DEFAULT_CACHE = Path.home() / ".cache" / "on-the-fly" / "models"


def streaming_languages() -> list[tuple[str, str]]:
    """The source languages the window may offer, in a stable order.

    Streaming tier *and* a pinned model. Offering the tier alone would put five languages
    in the picker that this project has not adopted a model for, and the user would find
    out after pressing Listen rather than before choosing (`app/catalogue.py`).
    """
    return [(lang.code, lang.name) for lang in recognisable_languages()]


def translation_options(source_language: str) -> list[tuple[str, str]]:
    """What `source_language` may be translated into, with captions-only offered first.

    The list is per source rather than fixed, because the pairs are: this project pins
    en->ru and ru->en, and nothing about a target picker built from the language table
    would say so.
    """
    options = [(NO_TRANSLATION, NO_TRANSLATION_LABEL)]
    options.extend((lang.code, lang.name) for lang in translation_targets(source_language))
    return options


def build_capture_stack(source: AudioSource) -> LevelWatchingSource:
    """Wrap a capture source in the two observers the window depends on, in that order.

    **The order is load-bearing.** Settling sits *under* the level monitor, so the verdict
    the window shows describes the microphone rather than the analog path powering up: a cold
    capture starts pinned at the rail, which reads as clipping and is not (ADR 0020).
    Reversed, the window would tell a user their microphone is broken for the first two
    seconds of every session — and the level monitor judges a recording on the worst window
    it contains, so that verdict would stand for the rest of the session too.

    Extracted from the Qt worker so the invariant can be asserted without a display. It was
    a comment, and this project has just spent a day finding out what comments guarantee.
    """
    return LevelWatchingSource(SettlingSource(source))


def build_worker() -> Any:
    """Define the worker inside a function, so importing this module needs no Qt."""
    from PySide6 import QtCore

    class PipelineWorker(QtCore.QObject):
        started = QtCore.Signal(str)  # detail line
        partial = QtCore.Signal(str)
        final = QtCore.Signal(str)
        translated = QtCore.Signal(str)
        attribution = QtCore.Signal(str)
        overflowed = QtCore.Signal(int)
        input_quality = QtCore.Signal(str)
        nothing_recognised = QtCore.Signal(bool)
        failed = QtCore.Signal(str)
        finished = QtCore.Signal()

        def __init__(self, source_language: str, target_language: str, cache_dir: Path) -> None:
            super().__init__()
            self._source = source_language
            self._target = target_language
            self._cache_dir = cache_dir
            self._stop = False

        def stop(self) -> None:
            """Ask the run to end. Checked between frames, so it takes effect promptly."""
            self._stop = True

        def run(self) -> None:
            try:
                self._run()
            except Exception as exc:
                # Nothing above this catches, and an exception escaping a worker thread
                # kills it silently. The user gets the reason instead (handbook: user-facing
                # errors must be useful but safe — the type and message, never a traceback).
                self.failed.emit(f"{type(exc).__name__}: {exc}")
            finally:
                self.finished.emit()

        def _run(self) -> None:
            from on_the_fly.app.pipeline import StreamingRun, translate_finals
            from on_the_fly.domain.audio.levels import InputQuality
            from on_the_fly.domain.audio.ports import Translator
            from on_the_fly.infrastructure import parallel
            from on_the_fly.infrastructure.asr.models import layout_for, resolve
            from on_the_fly.infrastructure.asr.sherpa_streaming import SherpaStreamingRecognizer
            from on_the_fly.infrastructure.audio import MicrophoneSource
            from on_the_fly.infrastructure.model_store import ModelStore
            from on_the_fly.infrastructure.translation import open_translator, resolve_engine

            # Everything that can be refused is refused before the microphone is opened.
            pin = resolve(f"streaming-{self._source}")
            # The default engine, which is CTranslate2: the desktop has no reason to run
            # the slower one, and the engine that exists for phones is chosen by the
            # command line rather than by a picker nobody on a desktop needs (ADR 0018).
            choice = (
                resolve_engine((self._source, self._target))
                if self._target != self._source
                else None
            )

            def load_recogniser() -> SherpaStreamingRecognizer:
                directory = ModelStore(self._cache_dir, allow_download=True).ensure(pin)
                built = SherpaStreamingRecognizer(directory, layout=layout_for(pin))
                # Warmed here rather than after the microphone opens: it needs no device,
                # and doing it on this thread is what lets it overlap the translation model.
                built.warm_up()
                return built

            def load_translator() -> Translator | None:
                if choice is None:
                    return None
                return open_translator(choice, self._cache_dir, allow_download=True)

            # Both at once. Nothing in one needs the other, and in series this is the whole
            # of the wait between pressing start and being able to speak — 5.2 s to 21.4 s
            # measured on 2026-09-09 against a 3 s target.
            self.started.emit(
                "loading models" if choice is not None else "loading recognition model"
            )
            recognizer, translator = parallel.both(load_recogniser, load_translator)
            if choice is not None:
                self.attribution.emit(choice.attribution)

            # Wrapped, so the frames the pipeline reads are the frames that get measured.
            # A microphone with its gain pinned produces fluent nonsense rather than
            # silence, and nothing downstream can tell (ADR 0019).
            source = MicrophoneSource()
            # Order is load-bearing and lives in `build_capture_stack`, where it is tested.
            watched = build_capture_stack(source)
            recognizer.validate_format(source.audio_format)
            self.started.emit(
                f"listening at {source.capture_rate_hz or source.audio_format.sample_rate_hz} Hz"
            )

            reported = InputQuality.OK
            reported_silence = False
            # Declared before the closure below refers to it: the module has a `run`
            # function, and without this the name resolves to that one.
            run: StreamingRun

            def report_state() -> None:
                """After every frame, whether or not it produced anything.

                Emitting only on change: a signal per frame would be a repaint per frame.
                Called from the run rather than from the event loop below, because a
                recogniser producing nothing produces no iterations of that loop either — so
                a wrong-language run used to go by with the window saying "listening" and not
                even its input-quality warnings updating (ADR 0045).
                """
                nonlocal reported, reported_silence
                current = watched.level.quality
                if current is not reported:
                    reported = current
                    self.input_quality.emit(current.value)

                silent = (
                    run.finals_so_far == 0
                    and run.speech_seconds >= SPEECH_WITHOUT_TEXT_IS_A_FINDING_SECONDS
                )
                if silent != reported_silence:
                    reported_silence = silent
                    self.nothing_recognised.emit(silent)

                if source.overflow_count:
                    self.overflowed.emit(source.overflow_count)

            run = StreamingRun(watched, recognizer, on_frame=report_state)

            def report_levels() -> None:
                report_state()

            events = run.events()
            stream = (
                translate_finals(
                    events,
                    translator,
                    source_language=self._source,
                    target_language=self._target,
                    store=run.store,
                )
                if translator is not None
                else None
            )

            if stream is None:
                for event in events:
                    if self._stop:
                        break
                    (self.final if event.is_final else self.partial).emit(event.text)
                    report_levels()
                    if source.overflow_count:
                        self.overflowed.emit(source.overflow_count)
            else:
                for item in stream:
                    if self._stop:
                        break
                    if item.is_final:
                        self.final.emit(item.event.text)
                        if item.translation:
                            self.translated.emit(item.translation)
                    else:
                        self.partial.emit(item.event.text)
                    report_levels()
                    if source.overflow_count:
                        self.overflowed.emit(source.overflow_count)

            watched.close()

    return PipelineWorker


def run(argv: Sequence[str] | None = None) -> int:
    """Start the desktop application. Returns the Qt exit code."""
    try:
        from PySide6 import QtCore, QtWidgets
    except ImportError:
        print(
            "The desktop interface needs PySide6, which is an optional extra:\n"
            "    pip install -r requirements-ui.txt",
        )
        return 1

    from on_the_fly.ui.window import build_window

    app = QtWidgets.QApplication(list(argv) if argv is not None else [])
    app.setApplicationName("on-the-fly")

    model = CaptionModel()
    state: dict[str, Any] = {"thread": None, "worker": None}
    window_ref: dict[str, Any] = {}

    def render() -> None:
        window_ref["window"].apply_state(model.state)

    def start(source_language: str, target_language: str) -> None:
        if state["thread"] is not None:
            return
        model.set_languages(source=source_language, target=target_language)
        render()
        model.starting()
        render()

        worker_cls = build_worker()
        worker = worker_cls(source_language, target_language, DEFAULT_CACHE)
        thread = QtCore.QThread()
        worker.moveToThread(thread)

        # Each handler updates the model, then renders. Kept as statements rather than
        # lambdas so the sequencing is visible and the type checker can see it.
        def on_started(detail: str) -> None:
            model.listening(detail=detail)
            render()

        def on_partial(text: str) -> None:
            model.partial(text)
            render()

        def on_final(text: str) -> None:
            model.final(text)
            render()

        def on_translated(text: str) -> None:
            model.translated(text)
            render()

        def on_attribution(text: str) -> None:
            model.set_attribution(text)
            render()

        def on_overflow(count: int) -> None:
            model.note_overflow(count)
            render()

        def on_input_quality(value: str) -> None:
            from on_the_fly.domain.audio.levels import InputQuality

            model.note_input_quality(InputQuality(value))
            render()

        def on_nothing_recognised(nothing: bool) -> None:
            model.note_nothing_recognised(nothing)
            render()

        def on_failed(reason: str) -> None:
            model.failed(reason)
            render()

        worker.started.connect(on_started)
        worker.partial.connect(on_partial)
        worker.final.connect(on_final)
        worker.translated.connect(on_translated)
        worker.attribution.connect(on_attribution)
        worker.overflowed.connect(on_overflow)
        worker.input_quality.connect(on_input_quality)
        worker.nothing_recognised.connect(on_nothing_recognised)
        worker.failed.connect(on_failed)

        def cleanup() -> None:
            thread.quit()
            thread.wait(3000)
            state["thread"] = None
            state["worker"] = None
            from on_the_fly.ui.caption import Status

            if model.state.status is not Status.FAILED:
                model.stopped()
            render()

        worker.finished.connect(cleanup)
        thread.started.connect(worker.run)
        state["thread"] = thread
        state["worker"] = worker
        thread.start()

    def stop() -> None:
        worker = state["worker"]
        if worker is None:
            return
        model.stopping()
        render()
        worker.stop()

    window = build_window(
        languages=streaming_languages(),
        targets_for=translation_options,
        on_start=start,
        on_stop=stop,
    )
    window_ref["window"] = window
    render()
    window.show()
    return int(app.exec())
