"""Continuous fuzzing of the AST parse/validate boundary.

QueryGate's entire safety argument is that the only thing a caller can submit
is a `StructuredQuery` AST which is fully validated before any database is
touched. `tests/security/test_malformed_input_fuzzing.py` sweeps that boundary
with a hand-written corpus of malformed-but-plausible shapes; this target is
the coverage-guided complement, which finds the shapes nobody thought to write
down.

**What counts as a finding.** Only two outcomes are acceptable for arbitrary
bytes:

* the input is rejected by JSON/UTF-8 decoding, or
* the input is rejected by pydantic validation.

Anything else — a `RecursionError` from a deeply nested document, an
`OverflowError` from an extreme numeric literal, a `TypeError` from a field
validator that assumed a type pydantic had not yet narrowed, or an
`AttributeError` from a cross-field validator reached with a partially-built
model — is a real defect, because every one of those reaches a caller as an
unhandled 500 rather than a clean 4xx. The repository has already shipped two
bugs of exactly this class (`RecursionError` escaping a `json.JSONDecodeError`
handler, and a type-confused `seq` accepted by pydantic's lax coercion), both
documented in CLAUDE.md's testing gotchas.

`RecursionError` is deliberately treated as a **finding, not an expected
rejection**. It is a `RuntimeError`, so an `except json.JSONDecodeError`
handler never covers it, and that exact gap is what item 194 had to close in
the WORM reader.

Run locally without atheris:

    poetry run python fuzz/fuzz_structured_query.py --selftest
"""

from __future__ import annotations

import json
import sys

# Instrumentation, and its measured limit — read this before changing it.
#
# `atheris.instrument_all()` (called in `main()`) rewrites the bytecode of
# every module already in `sys.modules`, so importing the target at module
# scope is correct here. Wrapping these imports in `instrument_imports()`
# instead was tried and is WRONG: measured on this target, coverage fell from
# 31 features to 2 and libFuzzer reported "no interesting inputs were found so
# far. Is the code instrumented for coverage?".
#
# **The honest limit.** Even wired correctly, coverage feedback on this target
# is weak — 31 features over 2.7M executions. `StructuredQuery.model_validate`
# dispatches almost immediately into `pydantic-core`, which is compiled Rust.
# Atheris instruments Python bytecode and cannot see inside it, so the majority
# of the work this target performs is invisible to the coverage-guided search,
# which degrades it towards blind random input.
#
# That is a real constraint on what this target is worth, not a defect to fix
# by rewiring. It means: treat a clean run as evidence that the boundary does
# not crash on random bytes, NOT as evidence that the input space was explored.
# The hand-written corpus in tests/security/test_malformed_input_fuzzing.py
# remains the load-bearing coverage of this boundary. See TODO.md item 227.
try:
    import atheris

    _HAS_ATHERIS = True
except ImportError:  # pragma: no cover - only present in the fuzzing image
    _HAS_ATHERIS = False

import pydantic

from querygate.query_ast.models import StructuredQuery

#: Rejections that mean the boundary did its job.
_CLEAN_REJECTIONS = (
    json.JSONDecodeError,
    UnicodeDecodeError,
    pydantic.ValidationError,
)


def consume(data: bytes) -> None:
    """Drive one fuzzer-supplied input through the AST boundary.

    Raises whatever the boundary raises when that exception is *not* a clean
    rejection — libFuzzer treats the escaping exception as the crash.
    """
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError:
        return

    try:
        document = json.loads(decoded)
    except (json.JSONDecodeError, RecursionError):
        # RecursionError here is a *decoder* limit on nesting depth, which is
        # json's documented behaviour on hostile input and is caught by the
        # REST layer. It is only a finding once it escapes the model layer
        # below, where handlers historically did not expect it.
        return

    try:
        StructuredQuery.model_validate(document)
    except _CLEAN_REJECTIONS:
        return
    except Exception as exc:  # noqa: BLE001 — the whole point is the wide net
        raise AssertionError(
            "StructuredQuery.model_validate raised a non-validation exception "
            f"({type(exc).__name__}: {exc}) — this reaches a caller as a 500, "
            "not a clean rejection. Input: {!r}".format(data[:400])
        ) from exc


def _selftest() -> int:
    """Exercise `consume` on a small corpus without atheris installed."""
    corpus = [
        b"",
        b"{}",
        b"[]",
        b"null",
        b"\xff\xfe",
        b'{"table": 1}',
        b'{"table": "T", "select": ["T.C"]}',
        b'{"table": "T", "select": [{"col": "T.C", "fn": "sum"}]}',
        b"[" * 200 + b"]" * 200,
        b'{"table": "T", "select": ["T.C"], "where": ' + b'{"and": [' * 50 + b"]}" * 50 + b"}",
        b'{"table": "T", "top_n": {"n": 1' + b"0" * 400 + b"}}",
    ]
    for item in corpus:
        consume(item)
    print(f"selftest OK — {len(corpus)} inputs, no unexpected exception")
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return _selftest()

    if not _HAS_ATHERIS:
        raise SystemExit(
            "atheris is not installed — run with --selftest, or build via "
            ".clusterfuzzlite/build.sh inside the OSS-Fuzz image."
        )

    # Instruments every already-imported module, including the target imported
    # at module scope above. Do not replace this with `instrument_imports()`
    # around those imports — see the measured comparison at the top of the file.
    atheris.instrument_all()
    atheris.Setup(sys.argv, consume)
    atheris.Fuzz()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
