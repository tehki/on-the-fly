"""The desktop window (ADR 0016).

Deliberately thin. Every decision about what appears on screen lives in `caption.py`, which
has no Qt in it and is tested without a display; this file turns a `ViewState` into widgets
and turns clicks into calls. If a behaviour is interesting enough to argue about, it belongs
next door, not here.

**PySide6 is imported lazily.** The pipeline, the CLI and the test suite must not need a GUI
toolkit installed, so nothing at module scope touches Qt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from on_the_fly.ui.caption import Status, ViewState

if TYPE_CHECKING:  # pragma: no cover - import shape only
    from collections.abc import Callable

# Dark, low-contrast surroundings with high-contrast text. Someone reading a live caption is
# looking at one thing, and everything else on screen is competing with it.
STYLE = """
QWidget#root {
    background: #14161a;
}
QLabel#source {
    color: #e8ecf1;
    font-size: 30px;
    font-weight: 500;
    line-height: 140%;
}
QLabel#source[partial="true"] {
    color: #7c8794;
}
QLabel#translation {
    color: #8fd6a4;
    font-size: 34px;
    font-weight: 600;
    line-height: 140%;
}
QLabel#status {
    color: #6b7684;
    font-size: 12px;
    letter-spacing: 1px;
}
QLabel#detail {
    color: #6b7684;
    font-size: 12px;
}
QLabel#attribution {
    color: #4a5460;
    font-size: 10px;
}
QLabel#warning {
    color: #e0a05a;
    font-size: 12px;
}
QPushButton#primary {
    background: #2f7d4f;
    color: #ffffff;
    border: none;
    border-radius: 6px;
    padding: 10px 26px;
    font-size: 14px;
    font-weight: 600;
}
QPushButton#primary:hover { background: #368c59; }
QPushButton#primary:disabled { background: #2a2f36; color: #5b636d; }
QPushButton#stop {
    background: #8c3a3a;
    color: #ffffff;
    border: none;
    border-radius: 6px;
    padding: 10px 26px;
    font-size: 14px;
    font-weight: 600;
}
QPushButton#stop:hover { background: #9e4444; }
QPushButton#stop:disabled { background: #2a2f36; color: #5b636d; }
QComboBox {
    background: #1c2027;
    color: #d5dbe3;
    border: 1px solid #2a2f36;
    border-radius: 5px;
    padding: 6px 10px;
    font-size: 13px;
}
QComboBox:disabled { color: #5b636d; }
QComboBox QAbstractItemView {
    background: #1c2027;
    color: #d5dbe3;
    selection-background-color: #2f7d4f;
}
"""

STATUS_TEXT = {
    Status.IDLE: "READY",
    Status.STARTING: "STARTING",
    Status.LISTENING: "LISTENING",
    Status.STOPPING: "STOPPING",
    Status.FAILED: "FAILED",
}


def build_window(
    *,
    languages: list[tuple[str, str]],
    on_start: Callable[[str, str], None],
    on_stop: Callable[[], None],
) -> Any:
    """Construct the main window.

    Takes its callbacks rather than reaching for a pipeline, so the window can be shown and
    driven in isolation — and so nothing in this file knows how translation works.
    """
    from PySide6 import QtCore, QtWidgets

    class MainWindow(QtWidgets.QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("on-the-fly")
            self.setMinimumSize(720, 400)

            root = QtWidgets.QWidget()
            root.setObjectName("root")
            self.setCentralWidget(root)
            outer = QtWidgets.QVBoxLayout(root)
            outer.setContentsMargins(28, 22, 28, 18)
            outer.setSpacing(14)

            # -- top bar: what it is doing, and between which languages ------------------
            top = QtWidgets.QHBoxLayout()
            self.status_label = QtWidgets.QLabel("READY")
            self.status_label.setObjectName("status")
            top.addWidget(self.status_label)
            top.addStretch(1)

            self.source_box = QtWidgets.QComboBox()
            self.target_box = QtWidgets.QComboBox()
            for code, name in languages:
                self.source_box.addItem(name, code)
                self.target_box.addItem(name, code)
            # Default to the pair that is actually pinned and measured, rather than
            # whatever sorts first — offering English to French by default would promise a
            # model this project has not adopted (ADR 0008).
            self._select(self.source_box, "en")
            self._select(self.target_box, "ru")
            arrow = QtWidgets.QLabel("→")
            arrow.setObjectName("detail")
            top.addWidget(self.source_box)
            top.addWidget(arrow)
            top.addWidget(self.target_box)
            outer.addLayout(top)

            # -- the caption: the only thing that matters while someone is speaking ------
            self.source_label = QtWidgets.QLabel("")
            self.source_label.setObjectName("source")
            self.source_label.setWordWrap(True)
            self.source_label.setAlignment(
                QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop
            )
            self.translation_label = QtWidgets.QLabel("")
            self.translation_label.setObjectName("translation")
            self.translation_label.setWordWrap(True)
            self.translation_label.setAlignment(
                QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop
            )
            self.translation_label.setTextInteractionFlags(
                QtCore.Qt.TextInteractionFlag.NoTextInteraction
            )

            outer.addWidget(self.source_label, 2)
            outer.addWidget(self.translation_label, 3)
            outer.addStretch(1)

            # -- footer: honesty about what is and is not happening ----------------------
            self.warning_label = QtWidgets.QLabel("")
            self.warning_label.setObjectName("warning")
            outer.addWidget(self.warning_label)

            bottom = QtWidgets.QHBoxLayout()
            self.detail_label = QtWidgets.QLabel("")
            self.detail_label.setObjectName("detail")
            bottom.addWidget(self.detail_label)
            bottom.addStretch(1)
            self.start_button = QtWidgets.QPushButton("Listen")
            self.start_button.setObjectName("primary")
            self.stop_button = QtWidgets.QPushButton("Stop")
            self.stop_button.setObjectName("stop")
            self.stop_button.setEnabled(False)
            bottom.addWidget(self.start_button)
            bottom.addWidget(self.stop_button)
            outer.addLayout(bottom)

            self.attribution_label = QtWidgets.QLabel("")
            self.attribution_label.setObjectName("attribution")
            self.attribution_label.setWordWrap(True)
            outer.addWidget(self.attribution_label)

            self.start_button.clicked.connect(self._start)
            self.stop_button.clicked.connect(lambda: on_stop())
            self.setStyleSheet(STYLE)

        def _start(self) -> None:
            on_start(self.source_box.currentData(), self.target_box.currentData())

        @staticmethod
        def _select(box: Any, code: str) -> None:
            index = box.findData(code)
            if index >= 0:
                box.setCurrentIndex(index)

        def apply_state(self, state: ViewState) -> None:
            """Apply a `ViewState`. The only way anything on screen changes.

            Not called `render`: `QWidget.render` already exists and paints the widget to a
            paint device, so overriding it here would have shadowed a Qt method with an
            incompatible signature.
            """
            self.status_label.setText(STATUS_TEXT.get(state.status, "—"))
            # The pickers follow the state rather than only feeding it. Without this the
            # window could show one language pair while translating another, which is the
            # kind of quiet disagreement this project keeps finding in its own documents.
            self._select(self.source_box, state.source_language)
            self._select(self.target_box, state.target_language)
            self.start_button.setEnabled(state.can_start)
            self.stop_button.setEnabled(state.can_stop)
            self.source_box.setEnabled(state.can_start)
            self.target_box.setEnabled(state.can_start)

            self.source_label.setText(state.caption.source)
            # Partials are dimmed rather than hidden: seeing words appear is the point of a
            # streaming recogniser, and the colour says "this may still change".
            self.source_label.setProperty("partial", "false" if state.caption.is_final else "true")
            self.source_label.style().unpolish(self.source_label)
            self.source_label.style().polish(self.source_label)

            self.translation_label.setText(state.caption.translation or "")
            self.detail_label.setText(state.detail)
            self.attribution_label.setText(state.attribution)

            if state.overflow_count:
                self.warning_label.setText(
                    f"{state.overflow_count} audio block(s) dropped — some speech was lost"
                )
            else:
                self.warning_label.setText("")

    return MainWindow()
