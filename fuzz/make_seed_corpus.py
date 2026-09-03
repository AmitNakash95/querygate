"""Generate a seed corpus of VALID StructuredQuery documents.

**Why this exists, measured rather than assumed.** `fuzz_policy_validation.py`
targets `validate_structural_caps`, which is pure Python and therefore fully
visible to Atheris — 18,128 functions instrumented. It still plateaued at **29
coverage features over 10,993,940 executions**, no better than the
pydantic-core-bound model target it was written to improve on.

The cause is not where the code is, it is that the code is unreachable.
Coverage-guided fuzzing from an empty corpus has to discover, byte by byte, a
document that satisfies a large pydantic model *before* the validator under test
runs even once. It never does: the corpus stalled at five inputs totalling seven
bytes. Every execution was rejected at the JSON or model gate.

A seed corpus fixes exactly that. Each seed is a valid `StructuredQuery` that
reaches `validate_structural_caps`, so the fuzzer starts *inside* the boundary
and mutates outward from there — flipping a depth, adding a CTE, deepening a
subquery — which is where the caps actually live.

Run by `.clusterfuzzlite/build.sh` at build time; the output is zipped to
`$OUT/fuzz_policy_validation_seed_corpus.zip`, the name ClusterFuzzLite and
OSS-Fuzz both look for.
"""

from __future__ import annotations

import json
import pathlib
import sys

#: Valid shapes spanning the structures the caps measure: CTEs
#: (`max_cte_count`) and nested subqueries (`max_subquery_depth`), plus the
#: ordinary shapes an agent actually sends. Kept literal rather than generated
#: from the test suite so a fixture change cannot silently empty the corpus.
_SIMPLE = {"from_table": "Customer", "select": ["Customer.Name"]}


def _nested(depth: int) -> dict:
    """Nesting via CTEs, `depth` levels down.

    There is no nested-`from_table`: the AST takes a table NAME there, which is
    itself part of the safety story. Depth in this AST comes from CTE bodies and
    predicate subqueries, so that is what the depth seeds exercise.
    """
    q: dict = dict(_SIMPLE)
    for i in range(depth):
        q = {
            "from_table": "Customer",
            "select": ["Customer.Name"],
            "ctes": [{"name": f"d{i}", "query": q}],
        }
    return q


def _with_ctes(count: int) -> dict:
    return {
        "from_table": "Customer",
        "select": ["Customer.Name"],
        "ctes": [{"name": f"c{i}", "query": dict(_SIMPLE)} for i in range(count)],
    }


SEEDS: list[dict] = [
    _SIMPLE,
    {"from_table": "Customer", "select": ["Customer.Name"], "limit": 10},
    {
        "from_table": "Customer",
        "select": ["Customer.Name"],
        "where": {"col": "Customer.Name", "op": "eq", "value": "x"},
    },
    {
        "from_table": "Customer",
        "select": [{"col": "Customer.Id", "fn": "count"}],
        "group_by": ["Customer.Name"],
    },
    {"from_table": "Customer", "select": ["Customer.Name"], "order_by": [{"col": "Customer.Name"}]},
    # Boundary-adjacent: the caps are what this target exists to exercise, so
    # seed both just-inside and at-the-edge shapes for the fuzzer to mutate past.
    _with_ctes(1),
    _with_ctes(3),
    _nested(1),
    _nested(2),
    _nested(3),
]


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: make_seed_corpus.py <output-dir>", file=sys.stderr)
        return 2
    out = pathlib.Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)

    # Fail loudly if a seed stops being valid: a corpus of rejects is worse than
    # no corpus, because it looks like coverage and provides none.
    from querygate.query_ast.models import StructuredQuery

    for i, seed in enumerate(SEEDS):
        StructuredQuery.model_validate(seed)
        (out / f"seed_{i:02d}.json").write_text(json.dumps(seed), encoding="utf-8")
    print(f"wrote {len(SEEDS)} validated seeds to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
