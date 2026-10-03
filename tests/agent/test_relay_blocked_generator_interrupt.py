"""Interrupting a Relay-managed provider read must not close the generator
the worker thread is still executing.

A real SIGALRM raises KeyboardInterrupt on the calling thread while the
provider blocks inside `next()` on a worker thread, then the stream is closed
explicitly. Before the fix, `ManagedLlmStream.close()` called `close()` on the
actively executing provider generator and raised
`ValueError: generator already executing` — and the generator's own `finally`
never ran.

Deferring the close hands it to whichever side is not executing the iterator. A
close claimed by the deferred path runs `close()` on the worker thread *after*
its read returns, so whatever that close raises lands on a thread with no caller
to propagate to: it is logged and swallowed. `test_failed_deferred_close_is_logged_not_raised`
pins that, so a provider whose `close()` fails cannot turn a deliberate
interrupt into a second, unrelated exception for the caller.

The remaining branch — a read that arrives after the close already claimed the
iterator, which returns `(None, True)` without entering it — is reachable
whenever a close lands in the window between the read being handed to the
executor and the worker entering the lock: the claim is taken at lock entry
rather than at submission, so the close wins, executes immediately, and the
worker then reads nothing. Reaching that interleaving deterministically from a
test requires a hook into the executor hand-off (`asyncio.to_thread`), which
this suite deliberately does not fake; the branch is guarded by the same claim
invariant as the deferred path, and a close losing that race is exactly what
the no-op read exists to absorb.
"""

from __future__ import annotations

import logging
import signal
import threading
import time

import pytest

pytest.importorskip("nemo_relay")

from agent import relay_llm, relay_runtime

pytestmark = pytest.mark.platforms("posix")  # SIGALRM / setitimer are POSIX-only


def _arm_when_entered(entered: threading.Event, delay: float = 2.0) -> None:
    """Arm SIGALRM only once the provider is confirmed inside ``next()``.

    Arming it up front makes the interrupt's arrival depend on how fast the
    executor starts the worker: on a loaded machine the alarm can fire before
    the provider has entered, and the handler's own precondition fails. Waiting
    on the provider's event makes the signal's start deterministic, and the
    ``disarm`` in the caller's ``finally`` still bounds every wait.
    """
    def arm() -> None:
        if entered.wait(10):
            signal.setitimer(signal.ITIMER_REAL, delay)

    threading.Thread(target=arm, daemon=True).start()


@pytest.fixture()
def relay_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    relay_runtime._reset_for_tests()
    lease = relay_runtime.SESSION_COORDINATOR.acquire_conversation(
        profile_key=relay_runtime.current_profile_key(),
        session_id="session-1",
        platform="cli",
    )
    turn = relay_runtime.SESSION_COORDINATOR.begin_turn(
        lease,
        turn_id="turn-1",
        task_id="task-1",
    )
    lease.host.retain_managed_execution("test.relay_blocked_provider_interrupt")
    try:
        yield lease.host.relay, turn
    finally:
        lease.host.release_managed_execution("test.relay_blocked_provider_interrupt")
        relay_runtime.SESSION_COORDINATOR.end_turn(turn, outcome="success")
        relay_runtime.SESSION_COORDINATOR.release_conversation(lease)
        relay_runtime._reset_for_tests()


def test_signal_during_blocked_provider(relay_turn):
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    workers, close_errors = [], []

    def provider(request):
        try:
            workers.append(threading.current_thread())
            entered.set()
            assert release.wait(10), "fixture release failed"
            yield {"choices": [{"index": 0, "delta": {"content": "done"}}]}
        finally:
            exited.set()

    def interrupt(signum, frame):
        assert entered.is_set(), "provider never started"
        raise KeyboardInterrupt("interrupt during provider read")

    previous = signal.signal(signal.SIGALRM, interrupt)
    # The release timer outlives the alarm, and the alarm is armed only once the
    # provider is confirmed inside next(); both bounds stay >= 2s so a loaded
    # machine cannot reorder them (AGENTS.md flake policy).
    timer = threading.Timer(8.0, release.set)
    timer.start()
    stream = None
    start = time.monotonic()
    try:
        _arm_when_entered(entered)
        with pytest.raises(KeyboardInterrupt):
            stream = relay_llm.stream(
                {"model": "test", "messages": [{"role": "user", "content": "hi"}]},
                provider,
                session_id="session-1",
                name="custom",
                model_name="test",
                metadata={"api_mode": "chat_completions"},
                finalizer=lambda: {},
            )
            next(stream)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        release.set()
        timer.join(10)
        if stream is not None:
            try:
                stream.close()
            except BaseException as exc:
                close_errors.append(repr(exc))
    assert stream is not None, "stream() did not return before the interrupt"
    assert time.monotonic() - start < 20
    assert not close_errors, close_errors
    assert exited.wait(3), "provider did not finish"
    for worker in workers:
        if worker is not threading.current_thread():
            worker.join(3)
    assert all(
        worker is threading.current_thread() or not worker.is_alive()
        for worker in workers
    )
    assert stream._loop is None
    assert stream._runtime_lease is None


class _FailingCloseIterator:
    """Iterator whose ``close()`` always fails, to pin the deferred-close contract."""

    def __init__(self, inner, error):
        self._inner = inner
        self._error = error
        self.close_calls = 0

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._inner)

    def close(self):
        self.close_calls += 1
        raise self._error


class _WarningWaiter(logging.Handler):
    """Capture matching WARNING records and signal on an Event, never by polling."""

    def __init__(self, marker):
        super().__init__(level=logging.WARNING)
        self.marker = marker
        self.messages = []
        self.logged = threading.Event()

    def emit(self, record):
        try:
            message = record.getMessage()
        except Exception:  # a broken record must not break the close path
            return
        if self.marker in message:
            self.messages.append(message)
            self.logged.set()


def test_failed_deferred_close_is_logged_not_raised(relay_turn):
    """A provider whose ``close()`` fails must not raise twice at the caller.

    The close is deferred to the worker because the interrupt caught the iterator
    mid-read, so the failure surfaces on that thread rather than in the caller's
    ``close()``. It is logged once and swallowed: an abandoned stream that already
    raised the caller's ``KeyboardInterrupt`` must not raise a second, unrelated
    exception on the way out.
    """
    entered, release = threading.Event(), threading.Event()

    def inner():
        entered.set()
        assert release.wait(10), "fixture release failed"
        yield {"choices": [{"index": 0, "delta": {"content": "done"}}]}

    iterator = _FailingCloseIterator(inner(), RuntimeError("provider close failed"))

    def provider(request):
        return iterator

    def interrupt(signum, frame):
        assert entered.is_set(), "provider never started"
        raise KeyboardInterrupt("interrupt during provider read")

    previous = signal.signal(signal.SIGALRM, interrupt)
    stream = None
    try:
        _arm_when_entered(entered)
        with pytest.raises(KeyboardInterrupt):
            stream = relay_llm.stream(
                {"model": "test", "messages": [{"role": "user", "content": "hi"}]},
                provider,
                session_id="session-1",
                name="custom",
                model_name="test",
                metadata={"api_mode": "chat_completions"},
                finalizer=lambda: {},
            )
            next(stream)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)

    assert stream is not None, "stream() did not return before the interrupt"
    assert entered.is_set(), "the provider read must still be in flight"
    waiter = _WarningWaiter("Deferred provider stream close failed")
    relay_logger = logging.getLogger("agent.relay_llm")
    relay_logger.addHandler(waiter)
    try:
        # The worker owns the iterator, so this close is deferred to it, and a
        # failure there has no caller to reach: the log record is the observable.
        stream.close()
        release.set()
        assert waiter.logged.wait(10), "the deferred close failure was never logged"
    finally:
        release.set()
        relay_logger.removeHandler(waiter)
    assert iterator.close_calls == 1, iterator.close_calls
    assert waiter.messages, "the deferred close failure was never logged"
