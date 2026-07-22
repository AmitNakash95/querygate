"""Reproducible adversarial security benchmark (TODO.md item 58, phase 1).

This turns QueryGate's internal adversarial QA assets (item 28's
`tests/security/` suite, the item 36 edge-case scenarios) into a *repeatable,
publishable* comparison: a fixed, versioned attack corpus is run against the
**real** QueryGate request-pipeline guardrails, and its catch rate is reported
next to a structurally-modeled raw-SQL-passthrough baseline plus the per-query
guardrail latency the enforcement adds.

Design constraints (why it looks the way it does):

- **Offline and deterministic.** Every guardrail this benchmark exercises —
  AST-only structural shape, policy allow/deny over every column reference, and
  compiler parameter binding — runs *before any database touch* in the real
  pipeline (`validation/policy_validation.py`, `compiler/sqlalchemy_compiler.py`).
  So the benchmark drives the genuine production code paths with no DB, no
  network, and no LLM, which is exactly what makes the published numbers
  reproducible by a third party. Pass/fail is fully deterministic; only the
  latency figures vary run to run and are reported as informational.
- **The baseline is a *declared structural model*, not a live LLM run.** Each
  case records whether a naive gateway that forwards a model-generated SQL
  string with no AST contract would block the same attack. That determination
  is structural (a string-concatenating passthrough has no per-query table/column
  policy and no parameter-binding contract), factual, and reproducible — see
  ``docs/business/SECURITY_BENCHMARK.md`` for the methodology and the honest
  scope of the comparison. A *live* Google MCP Toolbox / live-LLM baseline run
  is phase 2 (needs external infra) and is intentionally out of scope here.
- **Honest by construction.** Documented residual risks
  (``docs/INFERENCE_RISKS.md``) are carried in the corpus as ``policy_allow``
  cases and reported in their own section — the benchmark discloses what
  QueryGate does *not* block rather than cherry-picking only wins.

The check kinds are stateless single-purpose callables dispatched through the
``_CHECKS`` registry (the right-weight form of the composable-interfaces
doctrine for one-line variants — see ``CLAUDE.md``), so adding an attack class
means adding a corpus case, and adding a *check kind* means registering one
function.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional

import pydantic as pyd
import sqlalchemy as sa
import yaml

from querygate.compiler.sqlalchemy_compiler import compile_structured_query
from querygate.core.exceptions import PolicyViolationError, QueryValidationError
from querygate.policy.models import Policy
from querygate.query_ast.models import StructuredQuery
from querygate.validation.policy_validation import validate_policy

CheckKind = Literal["policy_block", "bound_parameter", "no_raw_sql_field", "policy_allow"]

# Input-position property names that would constitute a raw-SQL escape hatch on
# the request AST. `no_raw_sql_field` cases assert none of these ever appears as
# a property anywhere in the StructuredQuery JSON Schema — a structural
# regression lock on the core invariant that no caller-controlled SQL string
# reaches the database.
_RAW_SQL_ESCAPE_HATCH_NAMES = frozenset(
    {
        "sql",
        "raw_sql",
        "rawsql",
        "raw_query",
        "query_string",
        "querystring",
        "statement",
        "sql_text",
        "sql_expression",
        "expression",
        "snippet",
        "verbatim",
    }
)


class BenchmarkOutcome(pyd.BaseModel):
    """One party's expected verdict on a case (QueryGate or the baseline)."""

    blocks: bool = pyd.Field(description="True if this party blocks/neutralizes the attack.")
    guardrail: Optional[str] = pyd.Field(
        default=None, description="Name of the guardrail that blocks (QueryGate side)."
    )
    rationale: Optional[str] = pyd.Field(
        default=None, description="Why the baseline does or does not block."
    )


class SecurityBenchmarkCase(pyd.BaseModel):
    model_config = pyd.ConfigDict(extra="forbid")

    id: str
    category: str
    title: str
    check: CheckKind
    # `should_block` is the ground truth: True for a real attack an ideal
    # guardrail must stop; False for a documented-residual case that is allowed
    # by design (reported separately, never counted as a catch).
    should_block: bool = True
    dialect: str = "postgresql"
    payload: Optional[str] = None
    tables: Dict[str, List[str]] = pyd.Field(default_factory=dict)
    policy: Dict[str, Any] = pyd.Field(default_factory=dict)
    query: Dict[str, Any] = pyd.Field(default_factory=dict)
    querygate: BenchmarkOutcome
    raw_sql_baseline: BenchmarkOutcome


class SecurityBenchmarkCorpus(pyd.BaseModel):
    model_config = pyd.ConfigDict(extra="forbid")

    version: int
    corpus_id: str
    description: str
    cases: List[SecurityBenchmarkCase] = pyd.Field(min_length=1)

    @pyd.model_validator(mode="after")
    def _unique_ids(self) -> "SecurityBenchmarkCorpus":
        seen: set[str] = set()
        for case in self.cases:
            if case.id in seen:
                raise ValueError(f"duplicate benchmark case id: {case.id!r}")
            seen.add(case.id)
        return self


class CaseResult(pyd.BaseModel):
    id: str
    category: str
    title: str
    check: CheckKind
    should_block: bool
    querygate_blocked: bool
    querygate_expected_block: bool
    baseline_blocked: bool
    # True iff QueryGate's observed behavior matched its declared expectation —
    # a False here is a real regression, not a benchmark artifact.
    querygate_matched: bool
    detail: str
    latency_ms: float


class SecurityBenchmarkReport(pyd.BaseModel):
    corpus_id: str
    corpus_version: int
    # Attack cases only (should_block=True).
    attack_count: int
    querygate_caught: int
    baseline_caught: int
    querygate_catch_rate: float
    baseline_catch_rate: float
    # Documented-residual cases (should_block=False): allowed by design.
    residual_count: int
    # A regression signal: cases where QueryGate did not match its declared
    # expectation. Must be 0 for a healthy build.
    querygate_mismatches: int
    mean_guardrail_latency_ms: float
    p95_guardrail_latency_ms: float
    cases: List[CaseResult]

    @property
    def ok(self) -> bool:
        """A clean run: no regressions and every attack caught."""
        return self.querygate_mismatches == 0 and self.querygate_caught == self.attack_count


# --------------------------------------------------------------------------- #
# Check kinds — each returns (blocked: bool, detail: str). Stateless callables.
# --------------------------------------------------------------------------- #


def _build_tables(spec: Dict[str, List[str]]) -> Dict[str, sa.Table]:
    tables: Dict[str, sa.Table] = {}
    for name, columns in spec.items():
        tables[name] = sa.Table(
            name,
            sa.MetaData(),
            *[sa.Column(col, sa.String(255)) for col in columns],
        )
    return tables


def _check_policy_block(case: SecurityBenchmarkCase) -> tuple[bool, str]:
    query = StructuredQuery(**case.query)
    policy = Policy(**case.policy)
    try:
        validate_policy(query, policy, connection_id="benchmark")
    except (PolicyViolationError, QueryValidationError) as exc:
        return True, f"rejected by policy validation: {type(exc).__name__}"
    return False, "policy validation did not reject the query"


def _check_policy_allow(case: SecurityBenchmarkCase) -> tuple[bool, str]:
    # A documented residual: policy validation must NOT reject (allowed by
    # design). "blocked" is therefore expected False.
    query = StructuredQuery(**case.query)
    policy = Policy(**case.policy)
    try:
        validate_policy(query, policy, connection_id="benchmark")
    except (PolicyViolationError, QueryValidationError) as exc:
        return True, f"unexpectedly rejected: {type(exc).__name__}"
    return False, "allowed by design (documented residual)"


def _check_bound_parameter(case: SecurityBenchmarkCase) -> tuple[bool, str]:
    if case.payload is None:
        raise ValueError(f"case {case.id!r}: bound_parameter check requires `payload`")
    query = StructuredQuery(**case.query)
    policy = Policy(**case.policy)
    tables = _build_tables(case.tables)
    stmt, _limit = compile_structured_query(query, tables, policy, dialect=case.dialect)
    compiled = stmt.compile()
    sql_text = str(compiled)
    bound_values = [str(v) for v in compiled.params.values()]
    neutralized = case.payload not in sql_text and case.payload in bound_values
    if neutralized:
        return True, "payload carried as a bound parameter, absent from SQL text"
    if case.payload in sql_text:
        return False, "payload appeared verbatim in the compiled SQL text"
    return False, "payload was neither bound nor present (unexpected)"


def _iter_schema_property_names(schema: Dict[str, Any]) -> List[str]:
    """Every property name declared anywhere in a JSON Schema (top level + $defs
    + nested)."""
    names: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                names.extend(props.keys())
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return names


def _check_no_raw_sql_field(case: SecurityBenchmarkCase) -> tuple[bool, str]:
    schema = StructuredQuery.model_json_schema()
    names = {n.lower() for n in _iter_schema_property_names(schema)}
    leaked = sorted(names & _RAW_SQL_ESCAPE_HATCH_NAMES)
    if leaked:
        return False, f"raw-SQL escape-hatch field(s) present on the AST: {leaked}"
    return True, "no raw-SQL escape-hatch field exists anywhere in the request AST"


_CHECKS: Dict[CheckKind, Callable[[SecurityBenchmarkCase], tuple[bool, str]]] = {
    "policy_block": _check_policy_block,
    "policy_allow": _check_policy_allow,
    "bound_parameter": _check_bound_parameter,
    "no_raw_sql_field": _check_no_raw_sql_field,
}


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

DEFAULT_CORPUS = Path(__file__).resolve().parents[2] / "benchmarks" / "security_boundary_v1.yaml"


def load_corpus(path: str | Path) -> SecurityBenchmarkCorpus:
    with Path(path).open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    return SecurityBenchmarkCorpus(**data)


def _run_case(case: SecurityBenchmarkCase) -> CaseResult:
    check = _CHECKS[case.check]
    start = time.perf_counter()
    blocked, detail = check(case)
    latency_ms = (time.perf_counter() - start) * 1000.0
    return CaseResult(
        id=case.id,
        category=case.category,
        title=case.title,
        check=case.check,
        should_block=case.should_block,
        querygate_blocked=blocked,
        querygate_expected_block=case.querygate.blocks,
        baseline_blocked=case.raw_sql_baseline.blocks,
        querygate_matched=blocked == case.querygate.blocks,
        detail=detail,
        latency_ms=latency_ms,
    )


def run_security_benchmark(
    corpus: SecurityBenchmarkCorpus | None = None,
    *,
    path: str | Path | None = None,
) -> SecurityBenchmarkReport:
    """Run every corpus case through the real guardrails and aggregate a report."""
    if corpus is None:
        corpus = load_corpus(path or DEFAULT_CORPUS)

    results = [_run_case(case) for case in corpus.cases]

    attacks = [r for r in results if r.should_block]
    residuals = [r for r in results if not r.should_block]
    querygate_caught = sum(1 for r in attacks if r.querygate_blocked)
    baseline_caught = sum(1 for r in attacks if r.baseline_blocked)
    mismatches = sum(1 for r in results if not r.querygate_matched)

    latencies = sorted(r.latency_ms for r in results)
    mean_latency = sum(latencies) / len(latencies) if latencies else 0.0
    if latencies:
        p95_index = min(len(latencies) - 1, int(round(0.95 * (len(latencies) - 1))))
        p95_latency = latencies[p95_index]
    else:
        p95_latency = 0.0

    attack_count = len(attacks)
    return SecurityBenchmarkReport(
        corpus_id=corpus.corpus_id,
        corpus_version=corpus.version,
        attack_count=attack_count,
        querygate_caught=querygate_caught,
        baseline_caught=baseline_caught,
        querygate_catch_rate=(querygate_caught / attack_count) if attack_count else 0.0,
        baseline_catch_rate=(baseline_caught / attack_count) if attack_count else 0.0,
        residual_count=len(residuals),
        querygate_mismatches=mismatches,
        mean_guardrail_latency_ms=mean_latency,
        p95_guardrail_latency_ms=p95_latency,
        cases=results,
    )
