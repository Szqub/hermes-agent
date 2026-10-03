---
title: "Relay-Managed Stream Read/Close Ownership"
description: "Who owns the provider iterator in a Relay-managed stream: the read/close handshake in ManagedLlmStream and why a close is deferred rather than forced"
---

# Relay-managed stream read/close ownership

`agent/relay_llm.py::ManagedLlmStream` is the synchronous `Iterator` a caller
sees for a Relay-managed provider stream. It is the seam where an asyncio stream
meets a synchronous consumer, and it has two execution lanes:

- the **consumer thread** pulls chunks with `next(stream)`, which drives the
  Relay stream on a private event loop owned by the iterator; and
- a **worker thread** performs each individual provider read, because the read
  is a blocking `next()` on the provider's own iterator and must not run on the
  event loop (`asyncio.to_thread(_read_next_chunk)` in `_provider_stream`).

This page records who owns the provider iterator in each lane, what `close()`
may and may not do, and why the close is deferred rather than forced. Start here
before changing provider-read dispatch, stream teardown, or interrupt handling
in `agent/relay_llm.py`.

## Why the provider read leaves the loop

Relay pulls the next provider chunk before it hands over the current one, so a
blocking read on the loop withholds every chunk until the provider sends the
following one: text disappears for each provider pause and a steer aborts the
stream. The read therefore runs on a worker thread and the chunk is awaited.

The consequence is the ownership rule this page exists to state: **exactly one
thread may be inside the provider iterator at a time.** A worker may advance or
close it; the consumer lane may close it only while no worker is executing it.
A close must never run concurrently with a read.

## What goes wrong when the close races the read

An interrupt (a real `KeyboardInterrupt` from `SIGINT`/`SIGALRM`, a platform
stop, or any `GeneratorExit` delivered by `aclose()`) unwinds the consumer
lane. `ManagedLlmStream._close()` then awaits `aclose()` on the Relay stream,
whose `finally` used to call `close()` on the raw provider iterator
unconditionally.

A thread cannot be cancelled: when the interrupt lands while a worker is inside
`next()` on the provider iterator, that worker keeps executing. Closing the
iterator from the consumer lane in that window raises
`ValueError: generator already executing`, and — worse than the exception — the
provider generator's own `finally` never runs, so whatever cleanup the provider
owes is never performed. Suppressing the `ValueError` would leave the read
uncancelled and the cleanup unevidenced, which is why the fix is an ownership
handshake and not a `try/except`.

## The handshake

`_provider_stream` keeps one lock-guarded state (`close_guard` /
`close_state`) with three flags: `reading`, `close_requested`, and `closed`.

Both decisions are taken under that one lock, and that is the whole point:

- **`_claim_close()`** takes ownership of the close and returns whether the
  caller got it. Inside the lock it backs off when a worker is executing the
  iterator (recording `close_requested` instead), backs off when the iterator is
  already claimed, and otherwise claims it by setting `closed` — atomically with
  the check that no worker is reading.
- **`_read_next_chunk()`** claims the iterator the same way: under the lock it
  returns `exhausted` without touching the iterator when a close already claimed
  it, and sets `reading` otherwise. A worker that has already claimed `reading`
  is seen by `_claim_close()`, which defers instead of closing.
- **`_close_when_safe()`** is the only close entry point: it claims and, when it
  wins, closes once.
- A worker that finds `close_requested` when its read returns closes the
  iterator itself — it is the thread that was executing it, and it is suspended
  again now — and logs rather than raises, because the caller that requested the
  close has already returned.

The claim is what makes the two lanes exclusive. Deciding under the lock and
acting after releasing it is not enough: a worker can claim a read between the
closer's check and its `close()` call, and the close then lands on an executing
generator — the original bug, only narrower. Because the read's entry check reads
the same `closed` flag under the same lock, exactly one of the two claims can
win, and a close can never overlap a read.

## Consequences to preserve

- **A blocked read is bounded by the provider, not by the interrupt.** The
  deferred close happens when the provider's `next()` returns. That is the
  honest bound: forcing the close would corrupt the generator, and there is no
  portable way to cancel a thread mid-read.
- **The lease and the private loop are still released on the interrupted
  path.** `_close()` clears `_loop` and releases the runtime lease regardless of
  whether the provider close ran inline or was deferred.
- **No exception is surfaced from a deferred close.** It is logged; the caller's
  `close()` cannot wait for it without reintroducing the hang the deferral
  avoids.
- **`_next_provider_chunk` stays the read primitive.** The worker wrapper adds
  only the ownership bookkeeping; `StopIteration` handling is unchanged.

## Regression coverage

`tests/agent/test_relay_blocked_generator_interrupt.py` drives the real
`relay_turn` fixture with a real POSIX signal handler: the provider blocks, a
`SIGALRM` raises `KeyboardInterrupt` on the calling thread, and the test asserts
that the interrupt propagates, `close()` does not raise, the provider's
`finally` runs, every worker thread exits, and `_loop` / `_runtime_lease` are
released. It fails on the pre-fix code with
`ValueError: generator already executing` and passes after the handshake.
