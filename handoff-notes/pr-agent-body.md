## What does this PR do?

Interrupting a Relay-managed provider read no longer closes the provider iterator while a worker thread is still executing it.

`ManagedLlmStream` advances the raw provider iterator on a worker thread (`asyncio.to_thread`). A thread cannot be cancelled, so when Relay closes the stream mid-read — an interrupt, an abandoned turn, a steer that drops the request — the old `finally` called `close()` on a generator that was still running on that worker. CPython raises `ValueError: generator already executing` for that, and because the close raised, the provider's own `finally` never ran: the connection and its buffers stayed owned by a generator nobody could reach again.

This PR makes read and close claim the iterator under one lock, so the two decisions cannot overlap. A close that finds a worker reading is recorded as a request and deferred to that worker, which runs it on returning from `next()`; a read that finds the iterator already closed returns without entering it. The claim is taken in the same critical section as the read's entry, so "the worker already claimed it" and "the close already claimed it" are the only two outcomes — a close can no longer land in the window between deciding and acting. A failure of the deferred close is logged, never raised, because it happens on a thread with no caller to reach: an interrupted stream that already raised the caller's `KeyboardInterrupt` must not raise a second, unrelated exception on the way out.

## Related Issue

Fixes #132048

## Type of Change

- [x] 🐛 Bug fix (non-breaking change that fixes an issue)
- [ ] ✨ New feature (non-breaking change that adds functionality)
- [ ] 🔒 Security fix
- [ ] 📝 Documentation update
- [ ] ✅ Tests (adding or improving test coverage)
- [ ] ♻️ Refactor (no behavior change)
- [ ] 🎯 New skill (bundled or hub)

## Changes Made

- `agent/relay_llm.py` — in the Relay provider callback: `close_guard` / `close_state` (`reading`, `close_requested`, `closed`), `_claim_close()` (takes ownership of the close iff no worker is executing the iterator, otherwise records the request), `_close_when_safe()` (closes once, and only while the iterator is not executing), and `_read_next_chunk()` (claims the read under the same lock, releases it in a `finally`, and performs a deferred close there). The read is claimed at entry rather than when the work item is queued, so a cancelled queue item cannot leak the close.
- `tests/agent/test_relay_blocked_generator_interrupt.py` — two tests, both POSIX-only (`pytest.mark.platforms("posix")`, real `SIGALRM`): a real interrupt while the provider blocks inside `next()`, then an explicit `close()`, pinning that the close does not raise, the provider's `finally` runs, no worker is left executing and the stream releases its loop and runtime lease; and a provider whose `close()` fails, pinning that the deferred failure is logged and never raised at the caller.
- `website/docs/developer-guide/relay-managed-stream-ownership.md` — the ownership contract: which side closes, when, and what a deferred close reports.
- `website/sidebars.ts` — registers that page.

## How to Test

1. `HERMES_HOME=$(mktemp -d) pytest tests/agent/test_relay_blocked_generator_interrupt.py -q`
   Both tests pass. On the parent commit the first one fails with `ValueError: generator already executing` raised out of `stream.close()` and the provider's `finally` never runs.
2. `HERMES_HOME=$(mktemp -d) pytest tests/agent/test_relay_llm.py tests/agent/test_relay_blocked_generator_interrupt.py tests/agent/test_auxiliary_relay.py tests/agent/test_relay_nested_execution.py tests/agent/test_relay_tools.py tests/agent/test_relay_runtime_bounded_scope_ops.py tests/agent/test_relay_runtime_plugins.py tests/agent/test_relay_scope_pop_metadata.py tests/agent/test_relay_session_segments.py tests/agent/test_relay_atof_cwd.py tests/agent/test_relay_cwd.py tests/agent/test_aux_relay_progress_seam.py -q`
   `135 passed, 7 failed`. The 7 failures are `nemo_relay` plugin skew in this environment (`TypeError: initialize() got an unexpected keyword argument 'additional_plugins_toml'`), identical on the parent commit and unrelated to this change.
3. Interactively: start a turn against a local provider that pauses mid-stream, interrupt it (Ctrl-C), and confirm no `generator already executing` in `agent.log`/`errors.log` and no lingering provider connection.

## Checklist

### Code

- [x] I've read the [Contributing Guide](https://github.com/NousResearch/hermes-agent/blob/main/CONTRIBUTING.md)
- [x] My commit messages follow [Conventional Commits](https://www.conventionalcommits.org/) (`fix(scope):`, `feat(scope):`, etc.)
- [x] I searched for [existing PRs](https://github.com/NousResearch/hermes-agent/pulls) to make sure this isn't a duplicate
- [x] My PR contains **only** changes related to this fix/feature (no unrelated commits)
- [ ] I've run `pytest tests/ -q` and all tests pass — not run: the relay and auxiliary-relay neighbourhoods listed under *How to Test* were, and they are the affected surface
- [x] I've added tests for my changes (required for bug fixes, strongly encouraged for features)
- [x] I've tested on my platform: Linux 6.12 (x86-64), Python 3.13

### Documentation & Housekeeping

- [x] I've updated relevant documentation (README, `docs/`, docstrings) — `website/docs/developer-guide/relay-managed-stream-ownership.md`
- [ ] I've updated `cli-config.yaml.example` if I added/changed config keys — N/A
- [ ] I've updated `CONTRIBUTING.md` or `AGENTS.md` if I changed architecture or workflows — N/A
- [x] I've considered cross-platform impact (Windows, macOS) per the compatibility guide — the read/close handshake is plain `threading.Lock` + dict state and is platform-independent; only the regression tests are POSIX-only, because they need a real `SIGALRM`, and they carry `pytest.mark.platforms("posix")` rather than a bare `skipif`
- [ ] I've updated tool descriptions/schemas if I changed tool behavior — N/A

## For New Skills

N/A — not a skill.

## Notes for review

Three drafts are open on this issue (#132059, #132064, #132080). They are not duplicates of this one: this branch does not wait out a busy generator and does not drain the off-loop read by timing. It removes the window they work around, by making the read/close decision a single claimed state — a close can only be executed by a side that is not holding the iterator.

Three things worth your scrutiny, all stated rather than hidden:

- The guard has a branch that is not covered by a test: a read requested after the close already claimed the iterator returns `(None, True)` without entering it. It is reachable when a close lands between the read being handed to the executor and the worker taking the lock — the claim is taken at lock entry rather than at submission, so the close wins and executes immediately and the worker then reads nothing. Reaching that interleaving deterministically from a test needs a hook into `asyncio.to_thread`, which this suite does not fake; the branch is guarded by the same claim invariant as the deferred path.
- A failure inside the deferred close is logged and swallowed rather than stored for the caller. Storing it would surface a provider's close failure to a caller that is already unwinding from an interrupt, which is the second exception this fix exists to prevent.
- The regression tests arm `SIGALRM` only once the provider is confirmed inside `next()` (a helper thread waits on the provider's event), so the interrupt's timing does not depend on how quickly the executor starts the worker; every wait is bounded at 2s or more and synchronised on events.