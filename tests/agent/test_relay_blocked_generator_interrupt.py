"""Interrupting a Relay-managed provider read must not close the generator
the worker thread is still executing.

Regression for #132048.

SIGINT delivered with ``pthread_kill`` on POSIX or ``interrupt_main`` on
Windows raises KeyboardInterrupt on the calling thread while the
provider blocks inside `next()` on a worker thread, then the stream is closed
explicitly. Before the fix, `ManagedLlmStream.close()` called `close()` on the
actively executing provider generator and raised
`ValueError: generator already executing` — and the generator's own `finally`
never ran.

Deferring a Python generator's close hands it to the side not executing it. A
close claimed by the deferred path runs `close()` on the worker thread *after*
its read returns, so whatever that close raises lands on a thread with no caller
to propagate to: it is logged and swallowed. `test_failed_deferred_close_is_logged_not_raised`
pins that, so a generator whose `close()` fails cannot turn a deliberate
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

Non-generator SDK handles retain immediate close, which may be required to
unblock their pending read; the SDK test is an anti-regression passing on base.
"""

from __future__ import annotations

import _thread
import logging
import signal
import threading
import time

import pytest

pytest.importorskip("nemo_relay")

from agent import relay_llm, relay_runtime


def _interrupt_main_thread() -> None:
    if hasattr(signal, "pthread_kill"):
        # A real signal wakes POSIX select(); interrupt_main only sets a flag.
        signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)
    else:
        _thread.interrupt_main(signal.SIGINT)


def _interrupt_when_entered(
    entered: threading.Event, stream_ready: threading.Event,
) -> tuple[threading.Event, threading.Thread]:
    """Send only after the provider enters and the caller has assigned its stream.

    The caller disarms and joins the sender before restoring its SIGINT handler.
    Both waits are bounded so failed stream construction cannot leak a sender.
    After assignment, a short buffer lets the consumer enter its blocking read.
    """
    disarmed = threading.Event()

    def fire() -> None:
        if entered.wait(10) and stream_ready.wait(10) and not disarmed.wait(2):
            _interrupt_main_thread()

    sender = threading.Thread(target=fire, daemon=True)
    sender.start()
    return disarmed, sender


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

    previous = signal.signal(signal.SIGINT, interrupt)
    # The release timer outlives the interrupt, and the interrupt fires only once
    # the provider is confirmed inside next(); the event orders their start.
    timer = threading.Timer(8.0, release.set)
    timer.start()
    stream = None
    start = time.monotonic()
    stream_ready = threading.Event()
    disarm, sender = _interrupt_when_entered(entered, stream_ready)
    try:
        with pytest.raises(KeyboardInterrupt):
            stream = relay_llm.stream(
                {"model": "test", "messages": [{"role": "user", "content": "hi"}]},
                provider,
                session_id="session-1",
                name="custom",
                model_name="test",
                metadata={"api_mode": "chat_completions"},
                finalizer=dict,
            )
            stream_ready.set()
            next(stream)
    finally:
        disarm.set()
        stream_ready.set()
        sender.join(10)
        assert not sender.is_alive(), "SIGINT sender did not finish"
        signal.signal(signal.SIGINT, previous)
        release.set()
        timer.cancel()
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
    close_calls = []

    def inner():
        try:
            entered.set()
            assert release.wait(10), "fixture release failed"
            yield {"choices": [{"index": 0, "delta": {"content": "done"}}]}
        finally:
            close_calls.append(threading.get_ident())
            raise RuntimeError("provider close failed")

    iterator = inner()

    def provider(request):
        return iterator

    def interrupt(signum, frame):
        assert entered.is_set(), "provider never started"
        raise KeyboardInterrupt("interrupt during provider read")

    previous = signal.signal(signal.SIGINT, interrupt)
    stream = None
    stream_ready = threading.Event()
    disarm, sender = _interrupt_when_entered(entered, stream_ready)
    try:
        with pytest.raises(KeyboardInterrupt):
            stream = relay_llm.stream(
                {"model": "test", "messages": [{"role": "user", "content": "hi"}]},
                provider,
                session_id="session-1",
                name="custom",
                model_name="test",
                metadata={"api_mode": "chat_completions"},
                finalizer=dict,
            )
            stream_ready.set()
            next(stream)
    finally:
        disarm.set()
        stream_ready.set()
        sender.join(10)
        assert not sender.is_alive(), "SIGINT sender did not finish"
        signal.signal(signal.SIGINT, previous)

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
    assert len(close_calls) == 1, close_calls
    assert waiter.messages, "the deferred close failure was never logged"


class _CloseUnblocksIterator:
    """SDK-like handle: close from another thread is what releases next()."""

    def __init__(self):
        self.entered = threading.Event()
        self.closed = threading.Event()
        self.exited = threading.Event()
        self.close_calls = 0

    def __iter__(self):
        return self

    def __next__(self):
        self.entered.set()
        try:
            assert self.closed.wait(10), "close did not unblock the SDK read"
            raise StopIteration
        finally:
            self.exited.set()

    def close(self):
        self.close_calls += 1
        self.closed.set()


def test_interrupt_closes_sdk_handle_before_read_returns(relay_turn):
    """Anti-regression: the unfixed base already closes SDK handles immediately."""
    iterator = _CloseUnblocksIterator()

    def interrupt(signum, frame):
        assert iterator.entered.is_set(), "provider never started"
        raise KeyboardInterrupt("interrupt during SDK read")

    previous = signal.signal(signal.SIGINT, interrupt)
    stream = None
    stream_ready = threading.Event()
    disarm, sender = _interrupt_when_entered(iterator.entered, stream_ready)
    try:
        with pytest.raises(KeyboardInterrupt):
            stream = relay_llm.stream(
                {"model": "test", "messages": [{"role": "user", "content": "hi"}]},
                lambda request: iterator,
                session_id="session-1",
                name="custom",
                model_name="test",
                metadata={"api_mode": "chat_completions"},
                finalizer=dict,
            )
            stream_ready.set()
            next(stream)
        assert stream is not None
        stream.close()
        assert iterator.closed.is_set(), "SDK close must run while next() is blocked"
        assert iterator.exited.wait(3), "SDK read did not finish after close"
        assert iterator.close_calls == 1
        assert stream._loop is None
        assert stream._runtime_lease is None
    finally:
        disarm.set()
        stream_ready.set()
        sender.join(10)
        assert not sender.is_alive(), "SIGINT sender did not finish"
        signal.signal(signal.SIGINT, previous)
        iterator.closed.set()
        if stream is not None:
            stream.close()
