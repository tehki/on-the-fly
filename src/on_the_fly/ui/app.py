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

from on_the_fly.domain.languages import SUPPORTED, RecognitionTier
from on_the_fly.ui.caption import CaptionModel

if TYPE_CHECKING:  # pragma: no cover - import shape only
    from collections.abc import Sequence

DEFAULT_CACHE = Path.home() / ".cache" / "on-the-fly" / "models"


def streaming_languages() -> list[tuple[str, str]]:
    """The pairs the window may offer, in a stable order.

    Only streaming-tier languages: a live caption window is the wrong place to discover that
    a language runs several seconds behind (ADR 0007).
    """
    return [
        (lang.code, lang.name)
        for lang in sorted(SUPPORTED.values(), key=lambda item: item.name)
        if lang.tier is RecognitionTier.STREAMING
    ]


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
            from on_the_fly.domain.audio.levels import InputQuality, LevelWatchingSource
            from on_the_fly.infrastructure.asr import ModelStore
            from on_the_fly.infrastructure.asr.models import STREAMING_LAYOUTS, resolve
            from on_the_fly.infrastructure.asr.sherpa_streaming import SherpaStreamingRecognizer
            from on_the_fly.infrastructure.audio import MicrophoneSource
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

            self.started.emit("loading recognition model")
            model_dir = ModelStore(self._cache_dir, allow_download=True).ensure(pin)
            recognizer = SherpaStreamingRecognizer(model_dir, layout=STREAMING_LAYOUTS[pin.name])

            translator = None
            if choice is not None:
                self.started.emit("loading translation model")
                translator = open_translator(choice, self._cache_dir, allow_download=True)
                self.attribution.emit(choice.attribution)

            # Wrapped, so the frames the pipeline reads are the frames that get measured.
            # A microphone with its gain pinned produces fluent nonsense rather than
            # silence, and nothing downstream can tell (ADR 0019).
            source = MicrophoneSource()
            watched = LevelWatchingSource(source)
            recognizer.validate_format(source.audio_format)
            recognizer.warm_up()
            self.started.emit(
                f"listening at {source.capture_rate_hz or source.audio_format.sample_rate_hz} Hz"
            )

            run = StreamingRun(watched, recognizer)
            reported = InputQuality.OK

            def report_levels() -> None:
                """Emit only on change: a signal per frame would be a repaint per frame."""
                nonlocal reported
                current = watched.level.quality
                if current is not reported:
                    reported = current
                    self.input_quality.emit(current.value)

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

    window = build_window(languages=streaming_languages(), on_start=start, on_stop=stop)
    window_ref["window"] = window
    render()
    window.show()
    return int(app.exec())
