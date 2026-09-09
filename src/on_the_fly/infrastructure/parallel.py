"""Loading two models at once, because the wait before the first word is two waits.

`docs/PERFORMANCE_BUDGET.md` asks for **application start to ready in 3 s, hard limit 6 s**,
and measured on 2026-09-09 the shipped path takes 5.17 s for a direct pair on CTranslate2 and
**21.39 s for a bridged pair on ONNX**. Almost none of that is computation this project
performs: it is a recogniser being built, then a translation model being built, one after the
other, each mostly waiting on a runtime that releases the GIL while it works.

They do not depend on each other. Nothing in a translation model's construction needs the
recogniser to exist, and the two legs of a bridged pair (ADR 0037) do not need each other
either. So they are built at the same time, and the wait is the longer of the two rather than
their sum.

**What this is not.** It is not a thread pool for the running application: recognition and
translation stay on the path they were measured on, and ADR 0014's thread settings are
untouched. This runs during construction and is finished before the first frame of audio is
read.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor


def both[First, Second](
    first: Callable[[], First], second: Callable[[], Second]
) -> tuple[First, Second]:
    """Run both, wait for both, return both.

    If either raises, the exception is re-raised here — `first`'s in preference to `second`'s,
    so that "the recogniser could not be loaded" is the message a caller who asked for both
    sees, rather than whichever thread happened to finish first. The other call is always
    waited for before returning, so no thread outlives this function holding a half-built
    model.
    """
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="load") as pool:
        running_first = pool.submit(first)
        running_second = pool.submit(second)
        # Resolved in this order deliberately: `.result()` waits, so both are complete before
        # either exception is raised, and the first argument's failure is the one reported.
        first_error = running_first.exception()
        second_error = running_second.exception()
        if first_error is not None:
            raise first_error
        if second_error is not None:
            raise second_error
        return running_first.result(), running_second.result()
