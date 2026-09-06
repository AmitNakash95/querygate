"""No part of QueryGate calls its vendor. There is no exemption.

**The claim has reverted to its original absolute, and this file is what keeps
it honest.** Item 210 narrowed the guard to exempt `src/querygate/subscription/`
because the product was being sold as a proprietary subscription and that client
genuinely did call a licence API with four disclosed fields. The open-source
decision removed the subscription client from the gateway entirely: entitlement
issuance now lives only in the private control plane, which the gateway never
contacts.

So the absolute is back — **no outbound call to us, from anywhere in the shipped
source, ever. No kill switch, no time bomb, no check that can refuse to start or
block a query.** That is a stronger claim than the four-field disclosure it
replaces, and it is asserted here positively rather than assumed:
`test_the_subscription_exemption_is_empty` fails if any module becomes exempt
again.

Two things are deliberately kept rather than deleted with the client:

  1. **The exemption machinery, asserted empty.** Keeping it costs nothing and
     means the guard cannot be widened silently — re-arming it requires making a
     test that says "nothing is exempt" go red.
  2. **The payload-contract checker.** It is exercised on every run against
     planted synthetic sources, so it is live code, not dead code, and it
     re-arms automatically if anything resembling a client ever returns.

One trap, found the hard way when the client was removed: deleting the package
leaves `subscription/__pycache__/` behind, so `SUBSCRIPTION_PKG.exists()` stays
true while nothing ships from it. Every guard keyed off existence stayed armed
for a package that was gone. Use `_subscription_package_ships()`, which looks
for real `*.py` sources.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.security, pytest.mark.unit]

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "querygate"

# Hosts that would represent a call home. Any QueryGate-controlled domain is a
# phone-home by definition, whatever it is called in the code.
VENDOR_HOST_RE = re.compile(
    r"\b(?:[\w-]+\.)*(?:querygate|query-gate)\.(?:com|io|dev|net|org|ai|sh|app|cloud)\b",
    re.I,
)

# Vocabulary that unambiguously means calling home. Deliberately NOT bare
# "telemetry" or "beacon": both have benign local uses here — a catalog fixture
# uses `telemetry` as a table alias, and `admin/redis_observed_shapes.py`
# describes itself as "best-effort operator telemetry", which is local and is
# the operator's own data. A guard that fires on those would be turned off.
# No trailing \b: the point is to catch `LICENSE_SERVER_URL`, where the next
# character is a word character.
PHONE_HOME_TOKENS = re.compile(
    r"(?:phone_?home|call_?home|licen[cs]e_server|entitlement_(?:url|endpoint|server)|"
    r"activation_(?:url|endpoint|server)|usage_report|heartbeat_url)",
    re.I,
)

# "telemetry" only counts when it is pointed at something over the wire.
TELEMETRY_ENDPOINT_RE = re.compile(r"telemetry[\w_]*\s*[:=]\s*[\"']https?://", re.I)


#: The path the exemption *would* cover. It no longer exists: the subscription
#: client was removed with the open-source transition, and
#: `test_the_subscription_exemption_is_empty` fails if anything reappears here.
#: The constant is kept so the exemption cannot be re-created silently — adding
#: a module back has to make a red test green again, deliberately.
SUBSCRIPTION_PKG = SOURCE_ROOT / "subscription"

#: Retained as the payload-contract detector's fixture, NOT as a live
#: disclosure. The subscription client that transmitted these was removed when
#: the gateway went open-source, and there is no longer any outbound payload to
#: disclose — `test_the_subscription_exemption_is_empty` asserts exactly that.
#: The checker below is still exercised against planted sources on every run, so
#: it re-arms automatically if anything resembling a client ever returns.
DISCLOSED_PAYLOAD_FIELDS = frozenset({"org_id", "deployment_id", "connection_count", "seat_count"})

#: A subscription client is a handful of modules. If the exemption ever covers
#: more than this, something that is not a licence client has been moved under
#: it — which is how a path-scoped exemption becomes a global off-switch.
#:
#: **Derived, not guessed.** TODO.md item 211's package list is eight modules
#: (`models`, `verify`, `state`, `gate`, `sources`, `manager`, `cache`, `cli`)
#: plus `__init__.py` — `rglob("*.py")` counts that one — so nine, plus
#: `bootstrap.py`, plus one slot of head-room. The first draft of this bound was 8, which would have
#: tripped on item 211's *intended* implementation on its first commit; a
#: tripwire that fires before any drift has occurred only teaches people to
#: raise it. Raise this only against a written module list, never to get a
#: suite green.
#:
#: **Raised to 11 on 2026-08-28 (item 216), and the reason is the rule.** The
#: increment is `observability.py`, which is `gate.py` *split in two* — the
#: notice/coarse-signal/gauge read side moved out so `gate.py`'s "one function
#: the request path calls" docstring stays true. It is not new exempt surface:
#: `observability.py` opens no socket, names no host, and reads an in-memory
#: verdict. The rule this establishes: **this bound moves for a split or a
#: removal, never to admit new functionality under the exemption.** A module
#: that would do I/O belongs behind `sources.py`, which is already exempt and
#: already the only place a request is made.
_MAX_EXEMPT_MODULES = 11


def _is_subscription_module(path: Path) -> bool:
    return path == SUBSCRIPTION_PKG or SUBSCRIPTION_PKG in path.parents


def _python_sources(*, include_subscription: bool = True) -> list[Path]:
    paths = sorted(SOURCE_ROOT.rglob("*.py"))
    if include_subscription:
        return paths
    return [p for p in paths if not _is_subscription_module(p)]


def _subscription_package_ships() -> bool:
    """True only when the package has real Python sources.

    NOT `SUBSCRIPTION_PKG.exists()`. Deleting the package leaves
    `subscription/__pycache__/` behind, so the directory still exists while
    nothing ships from it — and every guard below that keys off existence
    silently stays armed for a package that is gone. Measured: that is exactly
    what happened when the subscription client was removed.
    """
    return SUBSCRIPTION_PKG.is_dir() and any(SUBSCRIPTION_PKG.rglob("*.py"))


def test_no_vendor_hostname_appears_anywhere_in_the_shipped_source():
    """The grep a reviewer will run, run for them and pinned."""
    offenders = []
    for path in _python_sources(include_subscription=False):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if VENDOR_HOST_RE.search(line):
                offenders.append(
                    f"{path.relative_to(SOURCE_ROOT.parent.parent)}:{number}: {line.strip()}"
                )
    assert not offenders, (
        "a QueryGate-controlled hostname appears in shipped source, which would make "
        "the licensing FAQ's 'no outbound calls to us, ever' false:\n" + "\n".join(offenders)
    )


def test_no_telemetry_or_licence_server_vocabulary_in_the_shipped_source():
    """Catches the intent before the endpoint exists.

    A constant named `LICENSE_SERVER_URL` is a phone-home whether or not it is
    populated yet, and this is the guard that has to survive item 197.
    """
    offenders = []
    for path in _python_sources(include_subscription=False):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if PHONE_HOME_TOKENS.search(line) or TELEMETRY_ENDPOINT_RE.search(line):
                offenders.append(f"{path.relative_to(SOURCE_ROOT.parent.parent)}:{number}")
    assert not offenders, "telemetry/licence-server vocabulary in shipped source: " + ", ".join(
        offenders
    )


#: Banned outright in shipped source, with no exemption for `subscription/`.
#:
#: `smtplib`/`aiosmtplib` are here because three documents — `docs/INSTALL.md`,
#: the licensing FAQ and `docs/PRODUCT_GUIDE.md` — promise the gateway has
#: no SMTP client and no address to send to; item 216 put the renewal email in
#: the vendor control plane specifically so that stays true. Until this line, it
#: was a three-document promise with nothing enforcing it.
_BANNED_OUTBOUND_MODULES = frozenset({"requests", "urllib3", "smtplib", "aiosmtplib", "email"})


def test_the_shipped_source_imports_no_outbound_http_client_at_module_scope():
    """The weakest of the three rules, and its limits are stated rather than implied.

    It bans `_BANNED_OUTBOUND_MODULES` outside the `allowed` set below. It
    does **not** ban `httpx` — that is a real dependency of the MCP SDK and of
    JWKS verification, both of which call hosts the *operator* configures, so a
    ban would fire on legitimate code every time. The consequence worth being
    honest about: a subscription client written with `httpx` (the natural choice,
    since it is already vendored) is unconstrained by this rule. The hostname and
    vocabulary rules above are what actually bound it, and inside
    `src/querygate/subscription/` neither applies — which is why
    `test_the_subscription_client_declares_only_the_disclosed_fields` exists.
    """
    allowed = {
        "core/auth.py",  # JWKS fetch, from an operator-configured issuer URL
        "core/jwks.py",
        "secrets/resolvers.py",  # Vault / cloud secret backends the operator names
        "health.py",
    }
    unexpected = []
    for path in _python_sources():
        rel = str(path.relative_to(SOURCE_ROOT))
        if rel in allowed:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - shipped source always parses
            continue
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                if name in _BANNED_OUTBOUND_MODULES:
                    unexpected.append(f"{rel} imports {name}")
    assert not unexpected, (
        "an unexpected HTTP client is imported in shipped source; if this is legitimate, "
        "add the module to `allowed` with the operator-configured endpoint it talks to:\n"
        + "\n".join(unexpected)
    )


def test_no_directory_symlink_can_hide_source_from_every_rule():
    """`Path.rglob` does not descend into a symlinked directory.

    So a symlinked package under `src/querygate/` is importable at runtime and
    **invisible to all three rules above** — not exempted, simply never scanned,
    which is a quieter hole than the `subscription/` exemption and has no bound
    on it at all. Cheaper to forbid the symlink than to make four scans follow
    links and then re-check containment.
    """
    links = [p for p in SOURCE_ROOT.rglob("*") if p.is_symlink()]
    assert not links, (
        "a symlink under src/querygate/ is not traversed by `rglob`, so its contents "
        "escape the phone-home scans entirely: "
        + ", ".join(str(p.relative_to(SOURCE_ROOT)) for p in links)
    )


def test_the_guard_would_catch_a_planted_beacon(tmp_path):
    """Negative control: the patterns must actually match a real phone-home."""
    planted = 'LICENSE_SERVER_URL = "https://api.querygate.com/v1/entitlement"'
    assert VENDOR_HOST_RE.search(planted)
    assert PHONE_HOME_TOKENS.search(planted)
    assert TELEMETRY_ENDPOINT_RE.search('TELEMETRY_URL = "https://api.querygate.com/t"')
    # ...and must not fire on the legitimate operator-configured shapes.
    for benign in (
        'issuer = "https://login.microsoftonline.com/tenant/v2.0"',
        'VAULT_ADDR = "https://vault.internal:8200"',
        'gateway = "https://gateway.internal"',
        # The two real in-tree uses of the word, both local to the operator.
        "aliases: [telemetry]",
        "which is the closer analogue: best-effort operator telemetry.",
    ):
        assert not VENDOR_HOST_RE.search(benign), benign
        assert not PHONE_HOME_TOKENS.search(benign), benign


# --- the subscription exemption, bounded --------------------------------------
#
# The two rules above stop at `src/querygate/subscription/`. That exemption is
# the only hole in the guard, so it gets its own guards: one on how wide it is,
# one on what may go over the wire through it.


def test_the_subscription_exemption_is_narrow():
    """A path-scoped exemption is safe only while the path stays small.

    Written while `subscription/` did not exist yet (item 211 is unstarted), so
    today this asserts the narrowing removes *nothing* from the guard's reach —
    the exemption cannot have silently pre-authorised a module that already
    shipped. It keeps asserting something real once the package lands.
    """
    exempt = set(_python_sources()) - set(_python_sources(include_subscription=False))
    # Expressed against the PATH STRUCTURE, not against `_is_subscription_module`.
    # The obvious `all(_is_subscription_module(p) for p in exempt)` is a tautology:
    # `exempt` is by construction the set that predicate selects, so the assertion
    # holds for any predicate at all, including `lambda p: True`. It shipped in the
    # first draft of this file and a reviewer caught it.
    top_level = {p.relative_to(SOURCE_ROOT).parts[0] for p in exempt}
    assert top_level <= {"subscription"}, (
        "the phone-home exemption reaches outside src/querygate/subscription/: "
        f"{sorted(top_level)}"
    )
    assert len(exempt) <= _MAX_EXEMPT_MODULES, (
        f"{len(exempt)} modules are exempt from the phone-home guard; a licence "
        f"client is not that big. Exempt: {sorted(p.name for p in exempt)}"
    )
    if not _subscription_package_ships():
        assert exempt == set(), "subscription/ does not ship, so nothing may be exempt"


def test_the_subscription_exemption_is_empty():
    """The gateway ships no vendor-calling client, so nothing may be exempt.

    This is the assertion that makes the module docstring's absolute checkable.
    Re-introducing any exempt module — a licence client, a telemetry beacon, an
    update checker — turns this red, which is the point: widening the guard has
    to be a deliberate act with a failing test in front of it, not a quiet
    addition under an exemption that already exists.
    """
    assert not _subscription_package_ships(), (
        "src/querygate/subscription/ ships Python again. The gateway is "
        "open-source and must not contain a vendor-calling client; entitlement "
        "issuance belongs in the private control plane."
    )
    exempt = set(_python_sources()) - set(_python_sources(include_subscription=False))
    assert exempt == set(), (
        f"{len(exempt)} module(s) are exempt from the phone-home guard, but the "
        f"exemption must be empty: {sorted(p.name for p in exempt)}"
    )


def test_the_exemption_is_path_scoped_and_not_a_pattern_hole():
    """The narrowing must be a *location* rule. If it had been done by loosening
    `PHONE_HOME_TOKENS` instead, a `LICENSE_SERVER_URL` anywhere in the tree
    would pass — which is the failure this file exists to prevent."""
    planted = 'ENTITLEMENT_URL = "https://api.querygate.com/v1/entitlement"'
    assert VENDOR_HOST_RE.search(planted) and PHONE_HOME_TOKENS.search(planted)
    assert _is_subscription_module(SUBSCRIPTION_PKG / "client.py")
    assert not _is_subscription_module(SOURCE_ROOT / "execution" / "service.py")
    assert not _is_subscription_module(SOURCE_ROOT / "subscription_helper.py")


def _payload_field_declarations(package: Path = SUBSCRIPTION_PKG) -> dict[str, frozenset[str]]:
    """Every module-level `PAYLOAD_FIELDS` in the subscription package.

    Accepts `X = {...}`, `X: frozenset[str] = {...}` (an `ast.AnnAssign`, which
    an `ast.Assign`-only walk misses entirely), and a `frozenset(...)`/`set(...)`
    wrapper — which is the idiom this very file uses for
    `DISCLOSED_PAYLOAD_FIELDS`, so an `Assign`-only literal check would have
    rejected its own house style. Sorted, and keyed by path, because the first
    draft overwrote a single variable while walking an **unsorted** `rglob`:
    with two declarations the winner was filesystem-order-dependent, which
    presents as a flake rather than as the finding it is.
    """
    found: dict[str, frozenset[str]] = {}
    for path in sorted(package.rglob("*.py")):
        rel = str(path.relative_to(package.parent))
        # `tree.body`, NOT `ast.walk`: walk descends into functions and class
        # bodies, so a shadowing `PAYLOAD_FIELDS` inside a request builder read
        # as a module-level declaration and — being keyed by file — silently
        # replaced the real one.
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.AugAssign):
                target = node.target
                assert not (isinstance(target, ast.Name) and target.id == "PAYLOAD_FIELDS"), (
                    f"{rel}:{node.lineno} augments PAYLOAD_FIELDS with `|=` or similar. "
                    "The disclosure must be a single literal; an augmented set is invisible "
                    "to this guard and is exactly how a fifth field would arrive."
                )
                continue
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not any(isinstance(t, ast.Name) and t.id == "PAYLOAD_FIELDS" for t in targets):
                continue
            value = node.value
            assert value is not None, f"{rel}:{node.lineno} annotates PAYLOAD_FIELDS with no value"
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id in {"frozenset", "set"}
            ):
                assert len(value.args) == 1, (
                    f"{rel}:{node.lineno} builds PAYLOAD_FIELDS from "
                    f"{len(value.args)} arguments; it must wrap one literal collection"
                )
                value = value.args[0]
            try:
                literal = ast.literal_eval(value)
            except ValueError as exc:  # a name, a union, a comprehension, an attribute
                raise AssertionError(
                    f"{rel}:{node.lineno} does not declare PAYLOAD_FIELDS as a literal "
                    f"({exc}). Accepted forms: `{{...}}`, `[...]`, `(...)`, or one of those "
                    "wrapped in `frozenset(...)`/`set(...)`, with or without an annotation. "
                    "The contract fixes a closed list, so the code must state one too — a "
                    "union, a comprehension or a name reference cannot be checked against it."
                ) from exc
            # Keyed by (file, line) so two declarations in ONE module are two
            # entries, not one silently overwriting the other.
            found[f"{rel}:{node.lineno}"] = frozenset(literal)
    return found


#: Header names a licence client legitimately writes as dict keys. Deliberately
#: four: every addition widens what may sit in a body-bound mapping inside the
#: exempt package, so an entry here is a decision, not a convenience.
#:
#: These are **keys**, not keyword-argument names. The first draft also listed
#: `url`, `json`, `data`, `content`, `params` and `timeout` — which are argument
#: names, already handled by `_BODY_KEYWORDS` — and the effect was to exempt a
#: payload field literally called `url`, so `{"org_id": o, "url": dsn}` passed
#: clean. A reviewer measured it.
_NON_PAYLOAD_KEYS = frozenset({"authorization", "user-agent", "content-type", "accept"})

#: Keyword arguments that carry a request body.
_BODY_KEYWORDS = frozenset({"json", "data", "content"})


def _is_mapping_literal(node: ast.AST) -> bool:
    """A dict written out in place, in either spelling.

    `{...}` and `dict(...)` are the same thing to a reader and to the wire, but
    only the first is an `ast.Dict`. The first draft of this contract keyed
    entirely on `ast.Dict`, so changing one token — `{**payload, "hostname": h}`
    to `dict(**payload, hostname=h)` — walked past all four rules at once.
    """
    if isinstance(node, ast.Dict):
        return True
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict"


def _is_mapping_merge(node: ast.AST) -> bool:
    """`{...} | extra` — the modern spelling of `{**a, **b}`.

    Same operation as rule 2's `**`, so it has to be the same violation.
    """
    return isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr)


def _mapping_keys(node: ast.AST) -> tuple[list[str], bool, bool]:
    """(literal keys, spreads another mapping, has an uncheckable key).

    **Recurses into nested mappings.** A body of
    `{"deployment_id": {"hostname": h}}` has only disclosed keys at the top
    level, so a flat read called it clean while `hostname` went over the wire
    one level down.
    """
    if _is_mapping_merge(node):
        left, _, left_computed = _mapping_keys(node.left)
        right, _, right_computed = _mapping_keys(node.right)
        return left + right, True, left_computed or right_computed

    literal: list[str] = []
    spread = False
    computed = False
    values: list[ast.AST] = []

    if isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values):
            if key is None:  # `{**other}`
                spread = True
            elif isinstance(key, ast.Constant) and isinstance(key.value, str):
                literal.append(key.value)
            else:
                computed = True
            values.append(value)
    elif isinstance(node, ast.Call):  # `dict(a=1, **rest)` / `dict(pairs)`
        for keyword in node.keywords:
            if keyword.arg is None:
                spread = True
            else:
                literal.append(keyword.arg)
            values.append(keyword.value)
        computed = bool(node.args)
    else:
        return [], False, False

    for value in values:
        if _is_mapping_literal(value) or _is_mapping_merge(value):
            nested, nested_spread, nested_computed = _mapping_keys(value)
            literal.extend(nested)
            spread = spread or nested_spread
            computed = computed or nested_computed
    return literal, spread, computed


#: Methods that add a key to an existing mapping. `BODY.update({...})` and
#: `BODY |= {...}` are rule 2's banned `**` spread with different syntax, applied
#: to a name already bound to a request body — the same class as the `dict()`
#: respelling, one level up. `_payload_field_declarations` already guards the
#: equivalent on the declared constant, which is why the omission here stood out.
_MUTATING_METHODS = frozenset({"update", "setdefault"})


def _body_mutations(tree: ast.Module, bound: set[str]) -> list[tuple[int, str]]:
    """Post-construction writes to a name that is sent as a request body."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id in bound
                and func.attr in _MUTATING_METHODS
            ):
                found.append((node.lineno, f"{func.value.id}.{func.attr}(...)"))
        elif isinstance(node, ast.AugAssign):
            if isinstance(node.target, ast.Name) and node.target.id in bound:
                found.append((node.lineno, f"{node.target.id} |= ..."))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id in bound
                ):
                    found.append((node.lineno, f"{target.value.id}[...] = ..."))
    return found


def _body_bound_names(tree: ast.Module) -> set[str]:
    """Names passed to a body keyword somewhere in this module."""
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg in _BODY_KEYWORDS and isinstance(keyword.value, ast.Name):
                    bound.add(keyword.value.id)
    return bound


def _body_bound_mappings(tree: ast.Module, bound: set[str]) -> list[ast.AST]:
    """Mapping expressions that can reach the wire in this module.

    Scoped deliberately. Applying the key rules to *every* dict in the package
    would fire on `cache.py`'s entries and a Pydantic `model_config`, so item
    211's first commit would go red for reasons that have nothing to do with
    disclosure — and the only relief would be widening the allowlist, which is
    how a bound becomes a rubber stamp.

    **Two shapes, and the limit is stated as a test, not only here** (see
    `test_a_body_assembled_in_another_module_is_not_reached`): a mapping passed
    straight to a body keyword, and one assigned to a name that is passed to a
    body keyword *in the same module*. A body built in one module and imported
    by another is out of reach of a per-module AST pass, as is one handed to a
    local helper that posts it. Those are the runtime assertion's job.
    """
    mappings: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg in _BODY_KEYWORDS and (
                    _is_mapping_literal(keyword.value) or _is_mapping_merge(keyword.value)
                ):
                    mappings.append(keyword.value)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if (
                node.value is not None
                and (_is_mapping_literal(node.value) or _is_mapping_merge(node.value))
                and any(isinstance(t, ast.Name) and t.id in bound for t in targets)
            ):
                mappings.append(node.value)
    return mappings


def payload_contract_violations(package: Path) -> list[str]:
    """Static check that the subscription client cannot transmit an undisclosed field.

    A pure function over a source tree, so it can be exercised against planted
    packages today rather than waiting for item 211 to make it runnable. Four
    rules, each closing a *named* bypass rather than a hypothetical one:

    1. **`PAYLOAD_FIELDS` is declared exactly once, as a literal, equal to the
       disclosed set.** The constant is the disclosure's counterpart in code.
    2. **No `**` unpacking in any dict literal.** This is the bypass a reviewer
       named against the declaration-only guard: `post(json={**payload,
       "hostname": ...})` satisfies rule 1 completely while putting a fifth field
       on the wire. A licence client has no legitimate use for dict-splat.
    3. **No dict literal passed as a request body.** The body must come from the
       declared constant or a model built from it, so that what rule 1 checks is
       what actually goes out.
    4. **Every string key in every dict literal is disclosed or transport
       plumbing**, and no dict literal uses a computed key — a key that is not a
       literal cannot be checked against the disclosure at all.

    Returns a list of human-readable violations. **Empty means no *mapping
    literal* reaching a request body carries an undisclosed field** — it does not
    mean the package cannot transmit one. A body built from a Pydantic model or
    a dataclass with a fifth attribute passes this cleanly, because a model field
    is an `ast.AnnAssign` in a `ClassDef`, not a mapping. That path is the
    runtime half, and TODO.md item 211 owns it: a schema-level assertion on the
    real request body, in the shape of `test_credential_redaction.py`. Saying so
    here rather than in a caveat elsewhere, because this is the sentence a
    reviewer quotes.
    """
    violations: list[str] = []
    declarations = _payload_field_declarations(package)

    if not declarations:
        violations.append(
            "no module-level `PAYLOAD_FIELDS` is declared; the contract fixes a closed "
            "list, so the code must state one that can be compared against it"
        )
    elif len(declarations) > 1:
        violations.append(
            f"PAYLOAD_FIELDS is declared {len(declarations)} times, so which one "
            f"describes the wire is ambiguous: {sorted(declarations)}"
        )
    else:
        where, declared = next(iter(declarations.items()))
        if declared != DISCLOSED_PAYLOAD_FIELDS:
            violations.append(
                f"{where} declares {sorted(declared)} but the contract fixes "
                f"{sorted(DISCLOSED_PAYLOAD_FIELDS)}; change the disclosure first"
            )

    for path in sorted(package.rglob("*.py")):
        rel = str(path.relative_to(package.parent))
        tree = ast.parse(path.read_text(encoding="utf-8"))

        bound = _body_bound_names(tree)
        for lineno, how in _body_mutations(tree, bound):
            violations.append(
                f"{rel}:{lineno} mutates a request body after it is built ({how}). That is "
                "the `**` spread with different syntax: whatever the mapping literal "
                "declares, the wire carries something else."
            )

        for mapping in _body_bound_mappings(tree, bound):
            literal, spread, computed = _mapping_keys(mapping)
            if spread:
                violations.append(
                    f"{rel}:{mapping.lineno} spreads another mapping into a request body "
                    "(`**`). That is exactly how an undisclosed field reaches the wire past "
                    "a declared PAYLOAD_FIELDS; build the body from the constant."
                )
            if computed:
                violations.append(
                    f"{rel}:{mapping.lineno} builds a request body with a key that is not a "
                    "string literal, so it cannot be checked against the disclosure at all"
                )
            for name in literal:
                if name.lower() in _NON_PAYLOAD_KEYS or name in DISCLOSED_PAYLOAD_FIELDS:
                    continue
                violations.append(
                    f"{rel}:{mapping.lineno} names {name!r} in a request body; it is neither "
                    "a disclosed payload field nor a header. Putting it on the wire is a "
                    "disclosure change before it is a code change."
                )

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg in _BODY_KEYWORDS and _is_mapping_literal(keyword.value):
                    violations.append(
                        f"{rel}:{node.lineno} passes a mapping literal as `{keyword.arg}=`. "
                        "The request body must be built from PAYLOAD_FIELDS so the declared "
                        "set and the transmitted set cannot diverge."
                    )
    return violations


# --- the contract's detectors, exercised against planted packages -------------
#
# `src/querygate/subscription/` does not exist yet, so the live check below can
# only skip. A guard that has never executed is a guard nobody can prove works —
# the same reasoning `test_eula.py` uses for its detector half. So the checker is
# a pure function over a directory, and these plant each bypass in `tmp_path` and
# require the rule to fire. When item 211 lands, the guard is known-good rather
# than never-run.

_COMPLIANT = 'PAYLOAD_FIELDS = {"org_id", "deployment_id", "connection_count", "seat_count"}\n'


def _plant(tmp_path, source: str, name: str = "client.py") -> Path:
    package = tmp_path / "subscription"
    package.mkdir(parents=True, exist_ok=True)
    (package / name).write_text(source, encoding="utf-8")
    return package


def test_a_compliant_client_passes_the_contract(tmp_path):
    """The positive control. Without it every detector below could be passing
    because the checker rejects everything."""
    source = _COMPLIANT + (
        "\n\ndef build(org, deployment, connections, seats):\n"
        "    return RefreshRequest(org_id=org, deployment_id=deployment,\n"
        "                          connection_count=connections, seat_count=seats)\n"
        "\n\nasync def refresh(client, body):\n"
        "    return await client.post(ENTITLEMENT_URL, json=body.model_dump(),\n"
        '                             headers={"authorization": token}, timeout=10)\n'
    )
    assert payload_contract_violations(_plant(tmp_path, source)) == []


def test_the_named_bypass_is_caught(tmp_path):
    """`post(json={**payload, "hostname": ...})` — satisfies a declared
    PAYLOAD_FIELDS completely while putting a fifth field on the wire. This is
    the exact hole the declaration-only guard could not see."""
    source = _COMPLIANT + (
        "\n\nasync def refresh(client, payload, host):\n"
        '    return await client.post(URL, json={**payload, "hostname": host})\n'
    )
    problems = payload_contract_violations(_plant(tmp_path, source))
    assert any("`**`" in p for p in problems), problems
    assert any("mapping literal as `json=`" in p for p in problems), problems


def test_the_same_bypass_spelled_with_dict_is_caught(tmp_path):
    """`dict(**payload, hostname=host)` is the identical bypass with one token
    changed. The first version of this contract keyed on `ast.Dict` alone and
    walked straight past it — a reviewer measured zero violations."""
    source = _COMPLIANT + (
        "\n\nasync def refresh(client, payload, host):\n"
        "    return await client.post(URL, json=dict(**payload, hostname=host))\n"
    )
    problems = payload_contract_violations(_plant(tmp_path, source))
    assert any("`**`" in p for p in problems), problems
    assert any("mapping literal as `json=`" in p for p in problems), problems


def test_an_undisclosed_field_spelled_with_dict_is_caught(tmp_path):
    source = _COMPLIANT + (
        "\n\nasync def refresh(client, org, host):\n"
        "    return await client.post(URL, json=dict(org_id=org, hostname=host))\n"
    )
    problems = payload_contract_violations(_plant(tmp_path, source))
    assert any("'hostname'" in p and "disclosure change" in p for p in problems), problems


def test_an_undisclosed_field_in_a_body_bound_dict_is_caught(tmp_path):
    """The indirect shape: a module-level body assembled once, sent elsewhere."""
    source = _COMPLIANT + (
        "\n\nBODY = {\n"
        '    "org_id": org,\n'
        '    "deployment_id": deployment,\n'
        '    "connection_count": connections,\n'
        '    "seat_count": seats,\n'
        '    "hostname": socket.gethostname(),\n'
        "}\n"
        "\n\nasync def refresh(client):\n"
        "    return await client.post(URL, json=BODY)\n"
    )
    problems = payload_contract_violations(_plant(tmp_path, source))
    assert any("'hostname'" in p and "disclosure change" in p for p in problems), problems


def test_a_computed_body_key_is_caught(tmp_path):
    """A key that is not a literal cannot be compared to the disclosure at all."""
    source = _COMPLIANT + (
        '\n\nBODY = {FIELD_NAME: value, "org_id": org}\n'
        "\n\nasync def refresh(client):\n"
        "    return await client.post(URL, json=BODY)\n"
    )
    problems = payload_contract_violations(_plant(tmp_path, source))
    assert any("not a string literal" in p for p in problems), problems


def test_mutating_a_body_after_it_is_built_is_caught(tmp_path):
    """`BODY.update({...})`, `BODY |= {...}` and `BODY["x"] = v` are rule 2's
    banned `**` spread in different syntax, applied to a name already bound to a
    request body. All three were clean while the rule that bans the operation
    they perform was in force — a reviewer measured it."""
    for mutation in (
        '    BODY.update({"hostname": h})\n',
        '    BODY |= {"hostname": h}\n',
        '    BODY["hostname"] = h\n',
        '    BODY.setdefault("hostname", h)\n',
    ):
        source = _COMPLIANT + (
            '\n\nBODY = {"org_id": org}\n'
            "\n\nasync def refresh(client, h):\n"
            + mutation
            + "    return await client.post(URL, json=BODY)\n"
        )
        problems = payload_contract_violations(_plant(tmp_path / mutation[:12].strip(), source))
        assert any("mutates a request body" in p for p in problems), (mutation, problems)


def test_a_merged_mapping_body_is_caught(tmp_path):
    """`{...} | extra` is the modern spelling of `{**a, **b}`."""
    source = _COMPLIANT + (
        "\n\nasync def refresh(client, extra, org):\n"
        '    return await client.post(URL, json={"org_id": org} | extra)\n'
    )
    problems = payload_contract_violations(_plant(tmp_path, source))
    assert any("`**`" in p for p in problems), problems


def test_a_nested_undisclosed_field_is_caught(tmp_path):
    """Only the top level was read, so a disclosed key carrying an undisclosed
    one inside it went over the wire clean."""
    source = _COMPLIANT + (
        "\n\nasync def refresh(client, org, h):\n"
        '    return await client.post(URL, json={"org_id": org, "deployment_id": {"hostname": h}})\n'
    )
    problems = payload_contract_violations(_plant(tmp_path, source))
    assert any("'hostname'" in p for p in problems), problems


def test_an_undisclosed_field_in_a_data_body_is_caught(tmp_path):
    """`_BODY_KEYWORDS` covers more than `json=`; nothing exercised the rest,
    so narrowing it to `{"json"}` left every detector green."""
    source = _COMPLIANT + (
        "\n\nasync def refresh(client, org, h):\n"
        '    return await client.post(URL, data={"org_id": org, "hostname": h})\n'
    )
    problems = payload_contract_violations(_plant(tmp_path, source))
    assert any("'hostname'" in p for p in problems), problems


def test_a_header_name_in_a_body_is_allowed_but_an_invented_one_is_not(tmp_path):
    """Both directions of `_NON_PAYLOAD_KEYS`.

    After the body-scoping fix, the plumbing control stopped reaching the
    allowlist at all — emptying `_NON_PAYLOAD_KEYS` left every detector green,
    so neither its current narrowness nor a future widening was checked.
    """
    allowed = _COMPLIANT + (
        '\n\nBODY = {"authorization": token, "content-type": ct, "org_id": org}\n'
        "\n\nasync def refresh(client):\n    return await client.post(URL, json=BODY)\n"
    )
    assert payload_contract_violations(_plant(tmp_path / "ok", allowed)) == []

    invented = _COMPLIANT + (
        '\n\nBODY = {"x-deployment-host": host, "org_id": org}\n'
        "\n\nasync def refresh(client):\n    return await client.post(URL, json=BODY)\n"
    )
    problems = payload_contract_violations(_plant(tmp_path / "bad", invented))
    assert any("'x-deployment-host'" in p for p in problems), problems


def test_a_body_assembled_in_another_module_is_not_reached(tmp_path):
    """The scope limit, asserted rather than only described.

    `_body_bound_mappings` is a per-module pass, so a body built in `payloads.py`
    and posted from `client.py` is out of its reach. Item 211's package splits
    `models.py`/`sources.py`/`manager.py`, so this is its *intended* shape — which
    is exactly why the limit is pinned here instead of left in a docstring, and
    why item 211's Definition of Done owns the runtime assertion.
    """
    package = _plant(
        tmp_path,
        _COMPLIANT + '\n\nBODY = {"org_id": org, "hostname": host}\n',
        name="payloads.py",
    )
    (package / "client.py").write_text(
        "from .payloads import BODY\n\n\nasync def refresh(client):\n"
        "    return await client.post(URL, json=BODY)\n",
        encoding="utf-8",
    )
    assert payload_contract_violations(package) == [], (
        "this test documents a known limit; if it now fails the checker got "
        "stronger — delete the test and say so in TODO.md item 211"
    )


def test_a_dict_unrelated_to_the_wire_is_not_flagged(tmp_path):
    """The false-positive control that keeps the bound honest.

    Item 211's package holds `cache.py`, `state.py` and `models.py`; each will
    have dict literals with nothing to do with the request body. A rule firing on
    those would turn that item's first commit red for a reason unrelated to
    disclosure, and the only relief would be widening the key allowlist — which
    is how a bound becomes a rubber stamp.
    """
    source = _COMPLIANT + (
        '\n\nmodel_config = {"extra": "forbid"}\n'
        '\n\nCACHED = {"plan": plan, "expires_at": expires, "status": "active"}\n'
        "\n\ndef merged(overrides):\n"
        "    return {**DEFAULTS, **overrides}\n"
    )
    assert payload_contract_violations(_plant(tmp_path, source)) == []


def test_a_declaration_that_disagrees_with_the_eula_is_caught(tmp_path):
    source = 'PAYLOAD_FIELDS = {"org_id", "deployment_id", "connection_count"}\n'
    problems = payload_contract_violations(_plant(tmp_path, source))
    assert any("the contract fixes" in p for p in problems), problems


def test_a_missing_or_duplicated_declaration_is_caught(tmp_path):
    assert any(
        "no module-level" in p for p in payload_contract_violations(_plant(tmp_path, "X = 1\n"))
    )
    duplicated = _plant(tmp_path / "dup", _COMPLIANT, name="a.py")
    (duplicated / "z.py").write_text(_COMPLIANT, encoding="utf-8")
    assert any("declared 2 times" in p for p in payload_contract_violations(duplicated))


def test_transport_plumbing_is_not_mistaken_for_a_payload_field(tmp_path):
    """The false-positive control. A guard that fires on `headers` gets deleted."""
    source = _COMPLIANT + (
        "\n\nHEADERS = {\n"
        '    "authorization": f"Bearer {key}",\n'
        '    "user-agent": agent,\n'
        '    "content-type": "application/json",\n'
        "}\n"
    )
    assert payload_contract_violations(_plant(tmp_path, source)) == []


def test_the_live_subscription_package_satisfies_the_contract():
    """Applies the proven checker to the real package.

    Skips only because item 211 has not created it. Every rule above is
    exercised against planted sources on every run, so the skip is a missing
    *subject*, not a missing *guard*.
    """
    if not _subscription_package_ships():
        pytest.skip("item 211 has not shipped; the contract is detector-tested above")
    assert payload_contract_violations(SUBSCRIPTION_PKG) == []


def test_the_subscription_client_declares_only_the_disclosed_fields():
    """Latent: no client ships, so this skips. Kept so it re-arms if one returns.

    **This checks the declaration, not the wire**, and the name says so
    deliberately: a client can satisfy it and still `post(json={**payload,
    "hostname": ...})`. The behavioural assertion — that the refresh request
    body's key set is exactly this — is item 211's own Definition of Done, built
    in the shape of `test_credential_redaction.py` rather than as a mock-call
    assertion. What lives here is the constant and the contract it pins, next
    to the exemption that makes the promise necessary.
    """
    if not _subscription_package_ships():
        pytest.skip(
            "item 211 has not shipped; DISCLOSED_PAYLOAD_FIELDS is its stated "
            "acceptance criterion (see TODO.md item 211)"
        )
    declarations = _payload_field_declarations()
    assert declarations, (
        "the subscription package must declare a module-level `PAYLOAD_FIELDS` "
        "naming every field it transmits, so the disclosure stays checkable"
    )
    assert len(declarations) == 1, (
        "more than one module declares PAYLOAD_FIELDS, so which one describes the "
        f"wire is ambiguous: {sorted(declarations)}"
    )
    declared = next(iter(declarations.values()))
    assert declared == DISCLOSED_PAYLOAD_FIELDS, (
        f"the declared payload is {sorted(declared)} but the contract fixes "
        f"{sorted(DISCLOSED_PAYLOAD_FIELDS)}; change the disclosure first"
    )
