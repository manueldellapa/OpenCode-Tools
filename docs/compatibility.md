# OpenCode compatibility

This document tracks which exact OpenCode version(s) OpenCode-Tools has
qualified, per ADR-005 (exact-version compatibility and agent identity proof)
and ADR-009 (POSIX platform baseline). It is an implementation-status record,
not a planning document: precedence for requirements still runs through the
PRD, System Design, and ADRs listed in `CLAUDE.md`.

## OpenCode `1.17.18`

**Status: candidate, not supported.**

`1.17.18` is the only version OpenCode-Tools targets in v0.1. It became a
candidate when the offline fixture pack in
[`tests/fixtures/opencode/1.17.18/`](../tests/fixtures/opencode/1.17.18/)
was built (work package M07-01, issue #21). A candidate version has offline
evidence for the transport, capability, and identity contract described in
System Design SS10, but has not yet been proven against a real, running
OpenCode process.

A version only moves from *candidate* to *supported* after:

1. the OpenCode adapter (`src/opencode_tools/opencode.py`, M07-02 through
   M07-06) is implemented and passes its full unit/component suite against
   this fixture pack, including exact-version selection, capability and
   control-plane preflight, the command builder and flag deny-list, the
   NDJSON transport decoder, sanitized-export agent identity verification,
   and the provider classifier;
2. the compatibility smoke test (M15-03, AC-027) runs a disposable,
   opt-in-only invocation against a real, locally installed `1.17.18` binary
   and inspects the resulting artifacts, with no automatic publication or
   GitHub mutation;
3. the exact-version registry is updated to enable `1.17.18` as a supported
   runtime version.

Until all three steps pass, the preflight in ADR-005 accepts no versions at
all: there is no fallback to "best effort," to a nearby version, or to a
default agent. If the identity proof or the smoke test ever fails for
`1.17.18`, the version reverts to unsupported and ADR-005 must be
re-reviewed; a fallback is not an acceptable workaround.

### Fixture pack

[`tests/fixtures/opencode/1.17.18/MANIFEST.json`](../tests/fixtures/opencode/1.17.18/MANIFEST.json)
is the provenance and integrity index for every fixture in the pack: each
entry records the fixture's category, positive/negative/trust-boundary
`kind`, requested agent `role` (when applicable), a SHA-256 digest of its
exact bytes, and a description. `tests/unit/test_opencode_adapter.py` proves
the pack's inventory, digests, and provenance metadata offline and
deterministically, without ever invoking OpenCode, a provider, or the
network. No fixture contains secrets, real model/provider identifiers, or
data captured from a live run.

Adding a new OpenCode version never edits this pack: it gets its own
`tests/fixtures/opencode/<version>/` directory and its own manifest, so the
`1.17.18` evidence and expectations stay immutable.

### Platform baseline

Per ADR-009, only macOS and Linux on a local POSIX filesystem are supported,
with Python 3.13+, Git, the GitHub CLI, and a qualified OpenCode version all
resolvable from `PATH`. Windows native and Windows-mounted WSL filesystems
are not supported; an experimental WSL setup on a Linux filesystem does not
by itself satisfy the v0.1 acceptance criteria.
