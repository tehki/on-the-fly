"""Desktop interface (ADR 0016).

The window is a thin renderer over `caption.CaptionModel`, which holds every decision about
what appears on screen and has no Qt in it. PySide6 is imported lazily and is an optional
extra: the pipeline, the command line and the test suite all run without a GUI toolkit
installed.
"""

from on_the_fly.ui.caption import Caption, CaptionModel, Status, ViewState

__all__ = ["Caption", "CaptionModel", "Status", "ViewState"]
