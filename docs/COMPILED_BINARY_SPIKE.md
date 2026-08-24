# Compiled-binary feasibility spike — TODO.md item 214

**Date:** 2026-08-24 · **Nuitka:** 4.1.3 · **Python:** 3.11.9 · **Host:** macOS arm64, clang 17

> **Verdict: technically GO. Legally BLOCKED pending counsel.**
>
> Nuitka compiles this codebase and the compiled binary behaves identically to
> the interpreter on three of four risk areas. The fourth found a **real bug in
> QueryGate** (now fixed) plus a build-flag requirement. But Nuitka 4.1.3
> declares **AGPLv3+**, which is a live question for a proprietary
> closed-source product and is not mine to answer — see §5.

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
| B | **SQLAlchemy** — dynamic dispatch, dialect lookup, statement rendering | emits correct Postgres SQL | byte-identical | ✅ survives |
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
3. **Cython** — BSD-licensed, no such question, but a much rougher fit for a
   whole application (it targets extension modules, not app freezing).
4. **PyInstaller** — GPLv2 *with an explicit exception* permitting proprietary
   bundling. Freezes rather than compiles, so it is weaker obfuscation: the
   bytecode is recoverable. Would satisfy "single binary in Docker" but not
   "obfuscated" to the same degree.
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

## 7. Build with `--include-package=querygate`

Result recorded on completion — see the commit that adds this section's table.

## 8. Recommendation

- **Proceed technically.** Three of four risk areas survive untouched, and the
  fourth was a genuine bug in QueryGate that is now fixed and guarded.
- **Do not proceed with Nuitka** until §5 is resolved by the owner. This is the
  gating item for the rest of item 214.
- **Keep the MCP fix regardless of the packaging outcome.** It is a real
  frozen-deployment bug with a real guard, independent of whether we ever
  compile.
- **Next steps if Nuitka is cleared:** run the full suite against a compiled
  build; build for Linux inside the Docker image; exercise the ODBC path; then
  measure startup, size and the support-debugging story.
