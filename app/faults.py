"""Deterministic failure injection (CON-6). The only place faults are defined.

Business faults are chosen by customer_id and work in every environment, including live.
Crash and hang faults are inert unless FAULT_CRASH / FAULT_HANG is set; deploy config must
never set either.
"""

import logging
import os
import threading

log = logging.getLogger(__name__)

PAYMENT_DECLINED = "fault-payment-declined"
STOCK_REJECTED = "fault-stock-rejected"
FAIL_AFTER_RESERVE = "fault-after-reserve"


def crash_point(process: str, point: str) -> None:
    """Exit the process abruptly, as a kill would, if FAULT_CRASH == '<process>:<point>'."""
    if os.environ.get("FAULT_CRASH") == f"{process}:{point}":
        log.critical("injected crash at %s:%s", process, point)
        # _exit skips finally blocks and atexit, so nothing gets committed or acked on the way out.
        os._exit(1)


def hang_point(process: str, point: str) -> None:
    """Block forever if FAULT_HANG == '<process>:<point>', so a test can kill the process here."""
    if os.environ.get("FAULT_HANG") == f"{process}:{point}":
        log.critical("injected hang at %s:%s", process, point)
        threading.Event().wait()
