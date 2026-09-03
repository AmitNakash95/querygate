"""Coverage-guided fuzzing of the structural-cap enforcement boundary.

**This is the target TODO.md item 227 asked for, and the reason it exists is a
measurement, not a hunch.** `fuzz_structured_query.py` targets
`StructuredQuery.model_validate`, which dispatches almost immediately into
`pydantic-core` — compiled Rust that Atheris cannot instrument. Measured: 31
coverage features over 2.7M executions, i.e. the coverage-guided search was
running close to blind.

`validate_structural_caps` is the opposite shape. It is **pure Python**, it is a
genuine enforcement point (`max_cte_count`, `max_subquery_depth`), it takes an
already-built AST plus a `Policy`, and it performs no I/O — no connection
registry, no `PolicyStore`, no database. Every branch it takes is visible to the
instrumentation.

**But visibility was not enough, and the measurement said so.** Targeting Python
code alone changed nothing: 29 features over 10,993,940 executions, no better
than the Rust-bound target. The validator was not invisible, it was
*unreachable* — random bytes essentially never satisfy the `StructuredQuery`
model, so the fuzzer never got past the gate and the corpus stalled at five
inputs totalling seven bytes.

`make_seed_corpus.py` fixes that, and it is what makes this target worth having:

    fuzz_structured_query (pydantic-core bound)     31 features
    fuzz_policy_validation, no seed corpus          29 features
    fuzz_policy_validation, WITH seed corpus       377 features   <-- 13x

Seeded, the corpus grows to 39 inputs / 8.9 KB and is still finding new coverage
at 4.6M executions. **If you change this target, re-measure — a fuzz target that
cannot reach its subject reports "no crashes" forever.**

**What counts as a finding.** The caps are allowed to reject — that is their
job, and `PolicyViolationError` is the shape of a correct rejection. Anything
else escaping is a defect, because a caller receives it as an unhandled 500
rather than a clean 4xx:

* a `RecursionError` from a deeply nested CTE or subquery chain — the cap exists
  precisely to stop unbounded nesting, so blowing the Python stack *while
  measuring depth* is the failure mode most worth hunting here;
* an `OverflowError`, `TypeError` or `AttributeError` from arithmetic or a walk
  over a shape the validator did not anticipate.

Run locally without atheris:

    poetry run python fuzz/fuzz_policy_validation.py --selftest
"""

from __future__ import annotations

import json
import sys

# See the instrumentation note in fuzz_structured_query.py: `instrument_all()`
# rewrites already-imported modules and is called in `main()`. Do NOT wrap these
# imports in `instrument_imports()` — measured strictly worse on the sibling
# target (2 features vs 31).
try:
    import atheris

    _HAS_ATHERIS = True
except ImportError:  # pragma: no cover - only present in the fuzzing image
    _HAS_ATHERIS = False

import pydantic

from querygate.core.exceptions import PolicyViolationError, QueryValidationError
from querygate.policy.models import Policy
from querygate.query_ast.models import StructuredQuery
from querygate.validation.policy_validation import validate_structural_caps

#: Rejections that mean the boundary did its job. `QueryValidationError` is
#: included because a cap may reject a shape the model accepted but the
#: validator considers structurally invalid; both reach a caller as a 4xx.
_CLEAN_REJECTIONS = (PolicyViolationError, QueryValidationError)

#: The default policy, built once. Deliberately NOT fuzzed alongside the query:
#: a fuzzer-chosen `max_subquery_depth` of 0 would reject everything and starve
#: the search of interesting inputs. The caps under test are the DEFAULTS, which
#: is what a deployment that never edits policy.yaml actually runs.
_POLICY = Policy()


def consume(data: bytes) -> None:
    """Drive one fuzzer-supplied input through the structural-cap validator."""
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return

    try:
        query = StructuredQuery.model_validate(document)
    except (pydantic.ValidationError, RecursionError):
        # Model-layer rejection is fuzz_structured_query.py's subject, not this
        # one. Getting here only means the fuzzer has not yet built a
        # well-formed AST; it is not a finding.
        return
    except Exception:
        # Also not this target's subject — the sibling target owns it, and
        # double-reporting the same crash from two harnesses wastes triage.
        return

    try:
        validate_structural_caps(query, _POLICY)
    except _CLEAN_REJECTIONS:
        return
    except Exception as exc:  # noqa: BLE001 — the wide net is the point
        raise AssertionError(
            "validate_structural_caps raised a non-policy exception "
            f"({type(exc).__name__}: {exc}) — this reaches a caller as a 500, "
            "not a clean rejection. Input: {!r}".format(data[:400])
        ) from exc


def _selftest() -> int:
    """Exercise `consume` on a small corpus without atheris installed."""
    # `from_table`, not `from`: an earlier revision of this corpus used the
    # wrong key, so every seed was rejected at the model gate and the selftest
    # passed while exercising nothing below it. Same failure the seed corpus
    # exists to fix, in miniature.
    deep_cte = {
        "from_table": "T",
        "select": ["T.C"],
        "ctes": [
            {"name": f"c{i}", "query": {"from_table": "T", "select": ["T.C"]}} for i in range(80)
        ],
    }
    corpus = [
        b"",
        b"{}",
        b"null",
        b'{"from_table": "T", "select": ["T.C"]}',
        b'{"from_table": "T", "select": ["T.C"], "ctes": []}',
        json.dumps(deep_cte).encode(),
        b'{"from_table": "T", "select": ["T.C"], "where": ' + b'{"and": [' * 40 + b"]}" * 40 + b"}",
        b"[" * 300 + b"]" * 300,
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
    atheris.instrument_all()
    atheris.Setup(sys.argv, consume)
    atheris.Fuzz()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
