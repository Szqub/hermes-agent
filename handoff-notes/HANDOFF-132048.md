# Handoff notes — #132048

Everything needed to finish, verify and publish the #132048 work lives in this
directory. It is on the `handoff/132048` branch only; the PR branch is
`fix/132048-relay-blocked-provider-close`.

Cloud work has no shared machine and no durable local disk, so the analysis and
the review rounds travel with the branch.

## State

    Szqub/hermes-agent
      fix/132048-relay-blocked-provider-close   b40ca5cf  THE PR BRANCH
      handoff/132048                            (this branch)

Target: open the PR against `NousResearch/hermes-agent` `main` for issue
**#132048** — *"[Bug]: Relay-managed plain-generator stream close races blocked
iterator after interrupt"* (`type/bug`, `comp/agent`, `P2`, `area/streaming`).

## What changed

`agent/relay_llm.py` — the managed stream no longer closes a generator that is
still executing in a worker thread. The close is deferred until the in-flight
`next()` returns, and the cancel path is bounded so an interrupt cannot block on
a generator that will never resume.

    agent/relay_llm.py                                        +81/-8
    tests/agent/test_relay_blocked_generator_interrupt.py     +246 (new)
    website/docs/relay-managed-stream-ownership.md            +105 (new)
    website/sidebars.ts                                       +1

## Verification

    scripts/run_tests.sh -j 1 --file-timeout 90 tests/agent/test_relay_blocked_generator_interrupt.py

-> **2 passed**.

The issue reproduces on the clean commit `bd0affe5` (3/3 canonical-runner runs,
no retries) with only the regression test added. The test uses the repository's
real `relay_turn` fixture, a synthetic blocking reader, and a real POSIX signal
handler raising `KeyboardInterrupt` on the calling thread — no mocks of the
close path.

Independent review: **APPROVE** (glm). Raw verdicts in `reviews/132048/`.

## Publishing

PR body: `pr-agent-body.md`. This repository's PR template has **no**
AI-disclosure section, so no `Model Used` decision is needed here (unlike the
hermes-webui side).

Both upstreams are read-only for the token on the authoring machine, so the PR
must be opened with write access:

    https://github.com/NousResearch/hermes-agent/compare/main...Szqub:fix/132048-relay-blocked-provider-close?expand=1

Author and signatory on every commit, PR, issue and comment is **Szqub** only.

## reviews/132048/

Per-issue verdicts from the independent review rounds (glm, kimi, delta).