"""Cooperative cancellation registry for research pipelines.

A neutral module that both app.py and research_agent.py can import without
creating an import cycle. A running pipeline is cancelled by adding its
company_id to a set; the pipeline checks this flag at stage boundaries and
before each expensive LLM/search call, and aborts at the next checkpoint.

This is cooperative cancellation: a network/LLM call already in flight when the
user clicks Stop will finish, but no further work (and no further tokens) is
spent after that. Nothing here force-kills a thread.
"""
import threading

_lock = threading.Lock()
_cancelled = set()


class PipelineCancelled(BaseException):
    """Raised inside a pipeline when the user requests a stop.

    Deliberately subclasses BaseException (not Exception) so the broad
    `except Exception` blocks scattered through the pipeline stages don't
    accidentally swallow the cancellation - it propagates straight up to the
    dedicated handler that marks the company as stopped.
    """


def request_cancel(company_id: str) -> None:
    """Flag a pipeline to stop at its next checkpoint."""
    if not company_id:
        return
    with _lock:
        _cancelled.add(str(company_id))


def is_cancelled(company_id: str) -> bool:
    with _lock:
        return str(company_id) in _cancelled


def clear(company_id: str) -> None:
    """Drop any cancel flag for this id (call when a fresh run starts)."""
    with _lock:
        _cancelled.discard(str(company_id))


def check(company_id: str) -> None:
    """Raise PipelineCancelled if a stop was requested for this company."""
    if is_cancelled(company_id):
        raise PipelineCancelled()
