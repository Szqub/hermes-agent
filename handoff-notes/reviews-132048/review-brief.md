You are an independent code reviewer. Review ONE commit in a local checkout. You are READ-ONLY: do not edit, stage, commit, push, or run anything that writes to the repository. Do not open pull requests. You MAY write throwaway probe files under /opt/data/cache/scratch/probes/ — never inside the repository.

Repository: /opt/data/cache/scratch/hermes-agent (checkout of NousResearch/hermes-agent)
Commit under review: 20f0a164 (branch fix/132048-relay-blocked-provider-close, parent bd0affe5)
Upstream issue: https://github.com/NousResearch/hermes-agent/issues/132048

Issue title: `[Bug]: Relay-managed plain-generator stream close races blocked iterator after interrupt`
Labels: type/bug, comp/agent, P2, area/streaming. Author's summary:
> On clean official commit `bd0affe5...`, interrupting a Relay-managed synchronous plain-generator stream while its `next()` is executing in a worker can cause `ManagedLlmStream.close()` to raise `ValueError: generator already executing`. Reproduced in 3/3 consecutive canonical-runner runs. Suspected cause: `ManagedLlmStream._provider_stream` now awaits `asyncio.to_thread(_next_provider_chunk, run_callback, raw_iterator)`. On cancellation its `finally` can invoke the raw generator's `close()` while the worker is still advancing it. Merely suppressing the exception would not establish that the blocked reader was cancelled.
> Expected behavior: propagate the interrupt and perform bounded cleanup without closing an actively executing Python generator; release the managed runtime lease and loop safely.

The reproduction test (added verbatim in the commit as tests/agent/test_relay_blocked_generator_interrupt.py) uses the real `relay_turn` fixture, a synthetic blocking reader, and a real POSIX signal handler raising KeyboardInterrupt on the calling thread.

READ AND TEST ENVIRONMENT (important):
- The repo has no `.venv` here. A borrowed interpreter exists at `/opt/data/cache/scratch/ha-venv/bin/python` (Python 3.13, pytest 9.1.1, and a `.pth` pointing at the installed Hermes venv's site-packages).
- Run tests with: `cd /opt/data/cache/scratch/hermes-agent && HERMES_HOME=/opt/data/cache/scratch/ha-home /opt/data/cache/scratch/ha-venv/bin/python -m pytest <files> -q -p no:cacheprovider`
- This environment's `nemo_relay` is older than the checkout expects, so some relay tests fail for environment reasons. Known pre-existing failures (they fail WITHOUT the commit too): tests/agent/test_relay_tools.py::test_request_rewrite_reaches_authorized_callback_once, four tests in tests/agent/test_relay_runtime_plugins.py, tests/agent/test_relay_atof_cwd.py::test_run_conversation_exports_session_and_turn_cwds. Do not report those as regressions, but DO check whether any NEW failure appears.

What to do:
1. `git show 20f0a164` and read the changed hunk in `agent/relay_llm.py` (`_provider_stream`, ~line 348).
2. Read the surrounding code: `_next_provider_chunk` (~292), `_close`, `_close_provider_resources`, `__next__`, `_preserve_pending_provider_chunks`, `_start_managed`, and `agent/relay_runtime.py` for lease semantics.
3. Read the repo rules: `AGENTS.md` (root and `agent/AGENTS.md`), `CONTRIBUTING.md`.
4. Reproduce: run `HERMES_HOME=/opt/data/cache/scratch/ha-home /opt/data/cache/scratch/ha-venv/bin/python -m pytest tests/agent/test_relay_blocked_generator_interrupt.py -q -p no:cacheprovider` (expect PASS). Then repeat the run 3 times.
5. Prove the test bites: `git stash push -- agent/relay_llm.py`, re-run the test (expect the `ValueError('generator already executing')` assertion failure), then `git stash pop`. Report exactly what you saw.
6. Attack the change. Specifically:
   - Is the lock-guarded `reading`/`close_requested`/`closed` handshake actually race-free? Look for interleavings that (a) close twice, (b) never close (leaked generator whose `finally` never runs), or (c) close while a worker is executing the iterator.
   - Does the deferred close leak a thread, a lease, or the private event loop on paths other than the one the test exercises? Consider `_preserve_pending_provider_chunks`, `__next__`'s StopAsyncIteration path, and normal exhaustion.
   - `_read_next_chunk` closes `raw_iterator`/`raw_stream` from the worker thread. Is `run_callback` safe to call there, and is the object being closed the right one?
   - Is the added `threading` import / new closure state consistent with the module's style and with `AGENTS.md` (no speculative infrastructure, comments keep the WHY)?
   - Does the doc page `website/docs/developer-guide/relay-managed-stream-ownership.md` describe the code as written (no invented behaviour)? Is the `website/sidebars.ts` edit correct?
   - Anything that would get this closed by a maintainer.

Rules: one logical change; claims must be backed by evidence. If you assert runtime behaviour, give the command and observed output, or mark it explicitly as unverified reasoning. Do not invent findings.

Output format (markdown, no preamble):
## Verdict
BLOCK / APPROVE WITH CHANGES / APPROVE — one line why.
## Verified claims
Commands you ran and what they returned (or "none — reasoning only").
## Findings
Numbered. Each: severity (blocker/major/minor/nit), file:line, what is wrong, why it matters, suggested fix.
## What is good
Short.
## Residual risk
What you could not verify.
