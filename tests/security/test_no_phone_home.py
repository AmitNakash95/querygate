"""No part of QueryGate's data plane calls its vendor — and the one part that
does calls it with four fields that are not the customer's data.

**The claim changed shape in item 210, and this file is what keeps the new one
honest.** The old absolute — "no outbound calls to us, ever ... no kill switch,
no time bomb, and no check that can refuse to start or block a query" — was true
of a product with no subscription. QueryGate is now sold as one, so items
211-213 add a client that *does* call the vendor and an entitlement check that
*can* refuse a query. Deleting this file at that point would have been the cheap
reaction, and it would have thrown away the two assertions that become *more*
valuable under a subscription, not less:

  1. **The gateway itself still never calls home.** A beacon originating in the
     request pipeline, the compiler, the catalog, or the audit sinks would
     contradict the product's central claim that credentials and data never
     leave — subscription or not. So the guard is *narrowed* to exempt exactly
     `src/querygate/subscription/`, and nothing else.
  2. **What the exempt module may transmit is enumerated, not trusted.**
     `docs/legal/EULA.en.md` §16.1 discloses four fields and §16.2 says no other
     transmission path exists. That disclosure is the mechanism
     `docs/business/GTM_SAAS.md` §8 sells, so it is pinned here as a positive
     assertion rather than left as prose.

Source-level rather than behavioural, deliberately: the claim is about code that
must *not* exist, and no runtime test can prove the absence of a call site it
never happens to execute. This is the posture
`test_every_key_the_script_touches_shares_one_hash_slot` takes for the same
reason.
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


#: The one package allowed to name the vendor — the subscription client of items
#: 211-213. Exempting a *package path* rather than loosening the patterns keeps
#: the hole one directory wide and greppable; the exemption's own width is
#: bounded by `test_the_subscription_exemption_is_narrow` below.
SUBSCRIPTION_PKG = SOURCE_ROOT / "subscription"

#: Exactly the fields `docs/legal/EULA.en.md` §16.1 discloses and
#: `docs/business/GTM_SAAS.md` §3 publishes (§8 handles the objection about
#: them; §3 is the publication). Item 211's outbound payload must
#: equal this set — adding a fifth field is a disclosure change before it is a
#: code change.
DISCLOSED_PAYLOAD_FIELDS = frozenset({"org_id", "deployment_id", "connection_count", "seat_count"})

#: A subscription client is a handful of modules. If the exemption ever covers
#: more than this, something that is not a licence client has been moved under
#: it — which is how a path-scoped exemption becomes a global off-switch.
#:
#: **Derived, not guessed.** TODO.md item 211's package list is eight modules
#: (`models`, `verify`, `state`, `gate`, `sources`, `manager`, `cache`, `cli`)
#: plus `__init__.py` — `rglob("*.py")` counts that one — so nine, plus one
#: slot of head-room. The first draft of this bound was 8, which would have
#: tripped on item 211's *intended* implementation on its first commit; a
#: tripwire that fires before any drift has occurred only teaches people to
#: raise it. Raise this only against a written module list, never to get a
#: suite green.
_MAX_EXEMPT_MODULES = 10


def _is_subscription_module(path: Path) -> bool:
    return path == SUBSCRIPTION_PKG or SUBSCRIPTION_PKG in path.parents


def _python_sources(*, include_subscription: bool = True) -> list[Path]:
    paths = sorted(SOURCE_ROOT.rglob("*.py"))
    if include_subscription:
        return paths
    return [p for p in paths if not _is_subscription_module(p)]


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
        "`docs/LICENSING_FAQ.md`'s 'no outbound calls to us, ever' false:\n" + "\n".join(offenders)
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


def test_the_shipped_source_imports_no_outbound_http_client_at_module_scope():
    """The weakest of the three rules, and its limits are stated rather than implied.

    It bans exactly `requests` and `urllib3` outside the `allowed` set below. It
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
                if name in {"requests", "urllib3"}:
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


#: The documents that publish the payload. **`EULA.he.md` is on this list and the
#: first draft omitted it** — deleting `seat_count` from the Hebrew disclosure
#: alone kept the whole suite green, in the language an Israeli customer signs.
_DISCLOSING_DOCUMENTS = (
    "docs/legal/EULA.en.md",
    "docs/legal/EULA.he.md",
    "docs/business/GTM_SAAS.md",
    "docs/LICENSING_FAQ.md",
    "docs/business/NORTH_STAR.md",
)

#: §16.1's own field list, in either language: the backticked identifiers inside
#: the clause. Both files write them identically (they are code identifiers), so
#: one pattern serves both.
_SECTION_16_1_RE = re.compile(r"\*\*16\.1 [^\n]*\*\*(.*?)(?=\n\*\*16\.1\(|\n\*\*16\.2)", re.S)
_BACKTICKED_RE = re.compile(r"`([a-z_]+)`")


def _section_16_1_fields(text: str) -> frozenset[str]:
    match = _SECTION_16_1_RE.search(text)
    assert match, "could not locate §16.1 — did the clause numbering change?"
    return frozenset(_BACKTICKED_RE.findall(match.group(1)))


def test_the_disclosed_fields_match_what_the_documents_publish():
    """The constant is only worth anything while it mirrors the disclosure.

    Enforcement ran one way — code must not exceed the documents. Nothing ran the
    other way, so amending `EULA.en.md` §16.1 to a fifth field left the whole
    suite green while the licence of record and the guard disagreed.

    **Set equality against §16.1's own identifiers**, not four substring checks
    plus a prose count. The substring form passed when a fifth field was added to
    a *different* clause, or named in a "what we never send" list, and the prose
    count (`"four fields"`) would have gone red on a lawyer writing "four (4)
    fields" — a false failure in a document the suite elsewhere pins as awaiting
    counsel.
    """
    root = SOURCE_ROOT.parent.parent
    for name in _DISCLOSING_DOCUMENTS:
        text = (root / name).read_text(encoding="utf-8")
        missing = [f for f in sorted(DISCLOSED_PAYLOAD_FIELDS) if f not in text]
        assert not missing, f"{name} does not name the disclosed field(s): {missing}"
    for name in ("docs/legal/EULA.en.md", "docs/legal/EULA.he.md"):
        declared = _section_16_1_fields((root / name).read_text(encoding="utf-8"))
        assert declared == DISCLOSED_PAYLOAD_FIELDS, (
            f"{name} §16.1 discloses {sorted(declared)} but the guard pins "
            f"{sorted(DISCLOSED_PAYLOAD_FIELDS)}. A change here is a disclosure "
            "change: amend both languages, the FAQ and NORTH_STAR together."
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
    if not SUBSCRIPTION_PKG.exists():
        assert exempt == set(), "subscription/ does not exist, so nothing may be exempt"


def test_the_exemption_is_path_scoped_and_not_a_pattern_hole():
    """The narrowing must be a *location* rule. If it had been done by loosening
    `PHONE_HOME_TOKENS` instead, a `LICENSE_SERVER_URL` anywhere in the tree
    would pass — which is the failure this file exists to prevent."""
    planted = 'ENTITLEMENT_URL = "https://api.querygate.com/v1/entitlement"'
    assert VENDOR_HOST_RE.search(planted) and PHONE_HOME_TOKENS.search(planted)
    assert _is_subscription_module(SUBSCRIPTION_PKG / "client.py")
    assert not _is_subscription_module(SOURCE_ROOT / "execution" / "service.py")
    assert not _is_subscription_module(SOURCE_ROOT / "subscription_helper.py")


def _payload_field_declarations() -> dict[str, frozenset[str]]:
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
    for path in sorted(SUBSCRIPTION_PKG.rglob("*.py")):
        rel = str(path.relative_to(SOURCE_ROOT))
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
                    "The EULA discloses a fixed list, so the code must state one too — a "
                    "union, a comprehension or a name reference cannot be checked against it."
                ) from exc
            # Keyed by (file, line) so two declarations in ONE module are two
            # entries, not one silently overwriting the other.
            found[f"{rel}:{node.lineno}"] = frozenset(literal)
    return found


def test_the_subscription_client_declares_only_the_disclosed_fields():
    """`docs/legal/EULA.en.md` §16.2 promises no other transmission path exists.

    **This checks the declaration, not the wire**, and the name says so
    deliberately: a client can satisfy it and still `post(json={**payload,
    "hostname": ...})`. The behavioural assertion — that the refresh request
    body's key set is exactly this — is item 211's own Definition of Done, built
    in the shape of `test_credential_redaction.py` rather than as a mock-call
    assertion. What lives here is the constant and its binding to the EULA, next
    to the exemption that makes the promise necessary.
    """
    if not SUBSCRIPTION_PKG.exists():
        pytest.skip(
            "item 211 has not shipped; DISCLOSED_PAYLOAD_FIELDS is its stated "
            "acceptance criterion (see TODO.md item 211)"
        )
    declarations = _payload_field_declarations()
    assert declarations, (
        "the subscription package must declare a module-level `PAYLOAD_FIELDS` "
        "naming every field it transmits, so the EULA disclosure is checkable"
    )
    assert len(declarations) == 1, (
        "more than one module declares PAYLOAD_FIELDS, so which one describes the "
        f"wire is ambiguous: {sorted(declarations)}"
    )
    declared = next(iter(declarations.values()))
    assert declared == DISCLOSED_PAYLOAD_FIELDS, (
        f"the declared payload is {sorted(declared)} but the EULA discloses "
        f"{sorted(DISCLOSED_PAYLOAD_FIELDS)}; change the disclosure first"
    )
