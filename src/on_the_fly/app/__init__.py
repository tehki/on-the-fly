"""Application layer: the composition root and the command line that drives it.

`domain/` decides what happens. `infrastructure/` talks to the outside world. This layer
is where the two are wired together, in one visible place, so that the security-sensitive
dependencies — the retention store and the reaper that enforces it — are impossible to
leave out by accident (handbook 4 and 41).
"""

from __future__ import annotations

from on_the_fly.app.pipeline import (
    DEFAULT_PROJECT_ID,
    PipelineResult,
    StreamingRun,
    StreamingStats,
    UtteranceRecord,
    build_store,
    run_capture,
)

__all__ = [
    "DEFAULT_PROJECT_ID",
    "PipelineResult",
    "StreamingRun",
    "StreamingStats",
    "UtteranceRecord",
    "build_store",
    "run_capture",
]
