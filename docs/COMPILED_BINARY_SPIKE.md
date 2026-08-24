# Compiled-binary feasibility spike — TODO.md item 214

**Date:** 2026-08-24 · **Nuitka:** 4.1.3 · **Python:** 3.11.9 · **Host:** macOS arm64, clang 17

> **Verdict: technically GO — all four risk areas clean. Legally BLOCKED
> pending an owner decision.**
>
> Nuitka compiles this codebase, and with one code fix and one build flag the
> compiled binary is **indistinguishable from the interpreter on all four risk
> areas** — same tool set, same argument-schema sizes (§7). Getting there found a
> **real bug in QueryGate** — a filesystem scan that shipped zero MCP tools
> silently in any frozen build (§4, now fixed and guarded).
>
> Nuitka 4.1.3 declares **AGPLv3+**, a live question for a proprietary product
> (§5). **But §9 supersedes that: use Cython + PyInstaller instead.** Both are
> licence-clean (Apache-2.0; GPLv2 with an explicit non-free-program exception,
> verified from package metadata), and the modules Cython *cannot* compile turn
> out to be the ones already published as OpenAPI/MCP schemas — so nothing worth
> protecting is lost. **No purchase and no counsel time needed.**
>
> Scope caveat, stated up front: this spike measured **four named risk areas on
> macOS arm64**, not the whole application, not the test suite against a
> compiled artifact, and not Linux/Docker/ODBC. §6 lists what that leaves open.

---

## 1. Why a spike and not an implementation

Item 214 makes the spike the first deliverable of the whole SaaS phase, because
the packaging plan is load-bearing for everything downstream: if this codebase
cannot be compiled, the "single obfuscated binary" in `docs/business/GTM_SAAS.md`
§6 changes shape, and so does the tamper-resistance argument in §6 of the plan.
Four risk areas were identified in advance, from CLAUDE.md's own documented
constraints rather than from guesswork.

## 2. Method

A probe script exercising each risk area, run interpreted for a baseline, then
compiled with `--standalone` and run again. Identical output means the area
survived; any divergence is the finding. Output is JSON so the two runs are
diffed rather than eyeballed.

The probe deliberately asserts on things that would still *look* fine if broken:
that pydantic **still rejects** invalid input (a compiled build that silently
stopped validating would be far worse than one that failed to start), and that
typed MCP tools have **non-empty** argument schemas rather than merely existing.

## 3. Results

| # | Risk area | Interpreted | Compiled | Verdict |
|---|---|---|---|---|
| A | **pydantic-core** — a Rust extension | validates; still rejects bad input | identical | ✅ survives |
| B | **SQLAlchemy** — dynamic dispatch, dialect lookup, statement rendering | emits correct Postgres SQL | identical SQL | ✅ survives |
| C | **MCP SDK** — forward-reference resolution at tool registration | **15 tools**, typed schemas resolved (3/5/4 properties) | **0 tools** | ❌ **broke — see §4** |
| D | **importlib.metadata** — console-script entry points | 12 scripts | 12 scripts | ✅ survives |

Build cost: ~4 minutes wall clock, **227 MB** standalone dist, 73 files.

## 4. The MCP finding — our bug, not the compiler's

`discover_and_register_tools()` globbed `*.py` on the filesystem relative to
`__file__`:

```python
tools_dir = Path(__file__).resolve().parent
for path in sorted(tools_dir.glob("*.py")):
    importlib.import_module(f"querygate.mcp.tools.{path.stem}")
```

A compiled build has no `.py` files on disk. The glob matched nothing, the
function returned `0`, and **the MCP server advertised zero tools — exit 0, no
exception, no warning.** An agent connecting to that deployment would simply see
no tools and conclude QueryGate does nothing.

Two things make this worth dwelling on:

- **It is not Nuitka-specific.** PyInstaller, a zipapp, or any zipimported wheel
  breaks identically. The bug predates the compilation plan; compilation only
  revealed it.
- **A less careful probe would have reported success.** The compiled run
  returned `all_typed_tools_have_properties: true`, because `all()` over an
  empty collection is `True`. The count is what caught it.

**Fixed** in `src/querygate/mcp/tools/__init__.py`: an explicit `TOOL_MODULES`
tuple replaces the scan, with `tests/unit/test_mcp_tool_registration.py`
asserting the tuple matches the directory in both directions, plus the
end-to-end assertion that was missing — that the server actually advertises
tools and that typed tools resolve non-empty schemas. The drift guard was
mutation-verified.

**After the fix, the failure became loud rather than silent**
(`ModuleNotFoundError: No module named 'querygate.mcp.tools.connections'`),
which is the correct behaviour but still a failure: the modules are not in the
binary, because `import_module(f"...{name}")` is invisible to static analysis
even with a static tuple driving it.

**The imports cannot simply be made static.** `discover_and_register_tools()`
calls `rebuild_recursive_ast_cycle(force=True)` *before* importing any tool
module, and that ordering is load-bearing: tool registration builds each tool's
JSON schema over the recursive model graph, and a shared process was previously
observed misattaching a `$ref`'s sibling `description` when the rebuild had not
run first. Module-scope imports would run at package-import time, before the
rebuild. The ordering is a real product requirement, so the **build** must
accommodate it.

**Therefore the build requires `--include-package=querygate`.** Result of that
build: see §7.

## 5. The licence finding — this one is blocking, and it is not a technical call

Nuitka 4.1.3 declares:

```
License: GNU Affero General Public License v3
Classifier: License :: OSI Approved :: GNU Affero General Public License v3 or later (AGPLv3+)
```

QueryGate's own dependency-licence gate caught this before I did —
`make license-check` refuses it as an unrecognised licence tier, which is the
gate working exactly as intended. **The Nuitka dependency was therefore reverted
and is not in `poetry.lock`.**

Why this matters here specifically: a compiler's licence does not normally reach
its output — but Nuitka **links its own runtime support code into the produced
binary**, and whether that makes the binary a derivative work of an AGPL work is
precisely the question. AGPL's network-use clause is the aggressive one for a
product customers self-host and reach over a network.

Also relevant: the build log reports `commercial grade 'not installed'`. Nuitka
sells **Nuitka Commercial** as a separate paid product aimed at closed-source
commercial users. The existence of that product is itself evidence the vendor
expects commercial users to buy a licence rather than rely on the AGPL edition.

**Options, none of which I should pick:**

1. **Buy Nuitka Commercial.** Most likely the intended path; cost and terms need
   checking, and it must explicitly permit closed-source distribution.
2. **Get counsel's opinion on the AGPL edition.** Cheaper if the answer is yes,
   expensive to be wrong about.
3. **Cython** — believed BSD-licensed, so no such question, but a much rougher
   fit for a whole application (it targets extension modules, not app freezing).
4. **PyInstaller** — believed GPLv2 *with an explicit exception* permitting
   proprietary bundling. Freezes rather than compiles, so it is weaker
   obfuscation: the bytecode is recoverable. Would satisfy "single binary in
   Docker" but not "obfuscated" to the same degree.

   ⚠️ **The licence characterisations in 3 and 4 are from memory, not verified.**
   Nothing in this repository can confirm them and I did not check the upstream
   projects. Confirm both before either is relied on — the whole point of §5 is
   that a build tool's licence is not a detail.
5. **Ship plain Python in the image** and rely on the EULA's anti-circumvention
   clause (item 210) plus the private repository. Weakest technically; zero
   licensing risk; and per `GTM_SAAS.md` §6 the contract was always the layer
   that actually holds.

**Recommendation:** price Nuitka Commercial first (option 1). If it is a few
thousand a year, it removes the question entirely and is cheap next to the
counsel time option 2 would consume.

## 6. What this spike did *not* test

Stated plainly so nobody reads more into it than it earns:

- **The whole application** — the probe exercises the four risk areas, not
  `create_app()`, not a served request, not the write path.
- **The test suite against a compiled build.** The real acceptance bar
  (item 214's DoD) is the full suite passing against the compiled artifact.
- **Linux / the actual Docker image.** This ran on macOS arm64. The shipped
  artifact is a Linux container; `pyodbc`/`aioodbc` against the MSSQL ODBC
  driver is the most likely place a native-extension problem shows up, and it
  was not exercised here at all.
- **Startup time and memory** of the compiled binary.
- **Debuggability.** Item 214 requires a documented support procedure for
  reading a stack trace out of a compiled build *before* shipping. Not started.

## 7. Build with `--include-package=querygate` — clean

Third build, with the MCP fix in place and the package included explicitly:

| Probe | Interpreted | Compiled | |
|---|---|---|---|
| A pydantic-core | (full result object) | identical | ✅ |
| B SQLAlchemy | (full result object) | identical | ✅ |
| C MCP tools | 15 tools; typed schemas 3/5/4 properties | **15 tools; 3/5/4** | ✅ |
| D console scripts | 12 | 12 | ✅ |

Tool *names* identical, and every tool's argument-schema **size** identical per tool
between the two runs — which is the assertion that actually matters, because a
tool can register with a truncated schema and still be counted. Dist 256 MB
(up from 227 MB, as expected: the whole `querygate` package is now included
rather than only what static analysis reached).

**So the build recipe is:**

```
--standalone --include-package=querygate
```

The `--include-package` flag is **not optional** and its absence is not
cosmetic: without it the tool modules are absent from the binary and
`discover_and_register_tools()` raises `ModuleNotFoundError` at startup. That is
a loud failure now — before the §4 fix, the same condition was silent. The build
must assert on it rather than trusting the flag to be remembered: the
compiled-artifact test in item 214's DoD should include "the compiled binary
advertises the same tool set as the interpreter", not merely "the binary runs".

## 8. Recommendation

- **Proceed technically.** All four risk areas are clean once the §4 fix and the
  §7 build flag are in place; the one failure was a genuine QueryGate bug, now
  fixed and guarded.
- **Do not proceed with Nuitka** until §5 is resolved by the owner. This is the
  gating item for the rest of item 214.
- **Keep the MCP fix regardless of the packaging outcome.** It is a real
  frozen-deployment bug with a real guard, independent of whether we ever
  compile.
- **Next steps if Nuitka is cleared:** run the full suite against a compiled
  build; build for Linux inside the Docker image; exercise the ODBC path; then
  measure startup, size and the support-debugging story.

---

## 9. Cython + PyInstaller — evaluated 2026-08-24, and it changes the recommendation

Proposed by the owner as a free alternative to Nuitka. Measured rather than
reasoned about, and the conclusion is more favourable than my §5 option-list
guessed.

### 9.1 Licences — clean, and verified this time

Read from the installed packages' own metadata, not from memory (§5's options 3
and 4 were written from recall, which was the wrong standard):

| Package | Declared licence |
|---|---|
| **Cython 3.3.0** | `Apache-2.0` — fully permissive |
| **PyInstaller 6.22.2** | `GPLv2-or-later with a special exception which allows to use PyInstaller to build and distribute non-free programs (including commercial ones)` |

PyInstaller's exception is stated in its own licence field, in those words. **So
this combination has no licence question at all** — the blocker that stops
Nuitka does not apply.

### 9.2 Two hard failures, both measured

**Pydantic models cannot be Cythonized.** Compiling `query_ast/models.py`
succeeds, but importing it raises at class-creation time:

```
PydanticUserError: A non-annotated attribute was detected:
  `as_expression = <cyfunction CaseSelectItem.as_expression>`
```

Cython compiles methods into `cyfunction` objects, which pydantic v2's
`ModelMetaclass` does not recognise as functions — so every method reads as an
un-annotated candidate *field*. This is fundamental, not a config nit.
**48 modules define `BaseModel` subclasses**, including all five model modules
that matter: `query_ast/models.py`, `write_ast/models.py`, `policy/models.py`,
`connections/models.py`, `audit/events.py`.

**Cython enforces annotations as runtime types.** `compiler/sqlalchemy_compiler.py`
compiled, then failed at import with
`TypeError: Expected str, got DatabaseDialect` on
`dialect: str = DatabaseDialect.POSTGRESQL` — despite `DatabaseDialect` being a
`StrEnum`. This repo uses annotations idiomatically, not as Cython type
declarations, so **`-X annotation_typing=False` is mandatory codebase-wide.**
With that directive the module compiled and **all 123 compiler tests passed
against the `.so`.** The directive costs most of Cython's *speed* benefit, which
is irrelevant here — we want opacity, not throughput.

### 9.3 Why this is better than it first looks

The obvious reading is "Cython protects the wrong half — the AST models are the
IP and they stay readable." That reading is **wrong**, and measuring it is what
changed the recommendation:

**The AST models are already public.** `create_app().openapi()` publishes **215
component schemas**, including `StructuredQuery` and every AST node
(`AggregateSelectItem`, `CaseSelectItem`, `ExpressionSelectItem`, …). The MCP
tool schemas publish the same graph. Any customer already has the complete AST
shape — it is the API contract. Compiling those modules protects nothing that
is not already in the open.

What is genuinely worth protecting is the **enforcement logic**: policy
validation, schema validation, the compiler, the session guardrails, and the
future subscription gate. Every one of those is a pure-logic module with no
`BaseModel`, and every one Cythonizes.

**One refactor stands in the way.** `execution/service.py` — the read
enforcement funnel, and where item 211's gate call site goes — defines 12
`BaseModel`s, so it cannot be compiled as-is. All twelve are result/response
types (`StructuredQueryResult`, `ExplainResult`, `VerdictResult`,
`BatchQueryItemResult`, the catalog-info trio, …) and all are already in the
published OpenAPI schema. Extracting them to `execution/results.py` would leave
`service.py` fully compilable. Same for `execution/write_execution.py`
(2 models).

### 9.4 Revised recommendation

**Prefer Cython + PyInstaller over Nuitka**, and stop waiting on the AGPL
question:

1. It is **free and licence-clean** — no purchase, no counsel time.
2. It protects **exactly the modules worth protecting**, and the ones it cannot
   protect are already published as schemas.
3. Selective compilation is arguably *better* than Nuitka's all-or-nothing: the
   protected set is an explicit, reviewable list rather than a side effect.

**Work it implies**, none of it blocked on anyone: extract response models out of
`execution/service.py` and `execution/write_execution.py`; add
`-X annotation_typing=False` and a per-module compile list to the build; verify
the full suite against a mixed `.so`/`.py` tree; then PyInstaller-bundle and
verify again.

**Still untested:** PyInstaller itself. §9 measured Cython only. The bundling
step, the mixed-extension tree, Linux/Docker, and the ODBC path all remain
open — see §6, which still applies.
