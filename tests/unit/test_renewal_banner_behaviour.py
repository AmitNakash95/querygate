"""Execute `renewal-banner.js` and assert what it actually renders.

**Why this file exists.** `test_renewal_banner_ui.py` asserts the *source* of the
banner, and a review demonstrated the limit of that: inverting `safeHref`'s
ternary, or replacing `escapeHtml` with the identity function, left every static
assertion green while turning the banner into an HTML-injection sink and letting
`javascript:` URLs into the `href`. Those two properties cannot be pinned by
reading strings out of a file; they need the code to run.

The harness is deliberately tiny — a DOM stub with the four methods the module
touches, driven through `node --test`, which the repo already uses for
`clients/typescript`. It is skipped rather than failed when Node is unavailable,
because the static file above still covers the structural half.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
BANNER_JS = REPO / "src" / "querygate" / "admin_ui" / "renewal-banner.js"

#: A DOM stub with exactly what the module touches: two elements looked up by id,
#: `innerHTML`/`textContent`/`hidden`/`dataset`, `querySelector` over the markup
#: it just wrote, `addEventListener`, `focus`, `setAttribute`, and a
#: `sessionStorage` that throws (the private-mode path).
_HARNESS = r"""
const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");

function makeElement(id) {
  return {
    id,
    innerHTML: "",
    textContent: "",
    hidden: false,
    dataset: {},
    _attrs: {},
    setAttribute(name, value) { this._attrs[name] = value; },
    removeAttribute(name) { delete this._attrs[name]; },
    focus() { this.focused = true; },
    addEventListener() {},
    querySelector() { return makeElement("child"); },
  };
}

function loadModule(storageThrows) {
  const banner = makeElement("renewal-banner");
  const announcement = makeElement("renewal-announcement");
  const elements = { "#renewal-banner": banner, "#renewal-announcement": announcement };
  global.document = { querySelector: (selector) => elements[selector] || null };
  global.window = {
    location: { origin: "https://gateway.example.invalid" },
    sessionStorage: storageThrows
      ? { getItem() { throw new Error("denied"); }, setItem() { throw new Error("denied"); },
          removeItem() { throw new Error("denied"); } }
      : { _v: {}, getItem(k) { return this._v[k] ?? null; },
          setItem(k, v) { this._v[k] = v; }, removeItem(k) { delete this._v[k]; } },
  };
  global.URL = URL;
  eval(fs.readFileSync(process.env.BANNER_JS, "utf8"));
  return { render: global.window.QueryGateRenewalBanner.render, banner, announcement };
}

const NOTICE = {
  severity: "critical",
  days_remaining: 3,
  grace_days_remaining: 17,
  expires_at: "2026-09-01T00:00:00Z",
  grace_expires_at: "2026-09-15T00:00:00Z",
  renewal_state: "cancelling",
  renewal_url: "https://billing.example.invalid/renew",
  headline: "Auto-renew is off; it ends in 3 days.",
  detail: "Queries keep working through a grace window.",
};

test("a javascript: renewal URL never reaches the href", () => {
  const { render, banner } = loadModule(false);
  render({ ...NOTICE, renewal_url: "javascript:alert(document.cookie)" });
  assert.ok(!banner.innerHTML.includes("javascript:"), banner.innerHTML);
  assert.ok(!banner.innerHTML.includes("<a "), "no link at all is the correct fallback");
});

test("a leading-whitespace javascript: URL is also rejected", () => {
  const { render, banner } = loadModule(false);
  render({ ...NOTICE, renewal_url: "  \tjavascript:alert(1)" });
  assert.ok(!banner.innerHTML.includes("javascript:"), banner.innerHTML);
});

test("a data: URL is rejected", () => {
  const { render, banner } = loadModule(false);
  render({ ...NOTICE, renewal_url: "data:text/html,<script>alert(1)</script>" });
  assert.ok(!banner.innerHTML.includes("data:"), banner.innerHTML);
});

test("an https renewal URL is kept", () => {
  const { render, banner } = loadModule(false);
  render(NOTICE);
  assert.ok(banner.innerHTML.includes("https://billing.example.invalid/renew"));
});

test("markup in a headline is escaped, not rendered", () => {
  const { render, banner } = loadModule(false);
  render({ ...NOTICE, headline: '<img src=x onerror="alert(1)">' });
  assert.ok(!banner.innerHTML.includes("<img"), banner.innerHTML);
  assert.ok(banner.innerHTML.includes("&lt;img"), banner.innerHTML);
  assert.ok(!banner.innerHTML.includes("onerror=\""), banner.innerHTML);
});

test("a quote in a detail cannot break out of an attribute", () => {
  const { render, banner } = loadModule(false);
  render({ ...NOTICE, detail: '" onmouseover="alert(1)' });
  assert.ok(!banner.innerHTML.includes('onmouseover="alert'), banner.innerHTML);
});

test("render(null) empties the banner and the announcement", () => {
  const { render, banner, announcement } = loadModule(false);
  render(NOTICE);
  assert.ok(banner.innerHTML.length > 0);
  render(null);
  assert.equal(banner.innerHTML, "");
  assert.equal(announcement.textContent, "");
  assert.equal(banner.hidden, true);
});

test("the live region receives one line, not the whole banner", () => {
  const { render, announcement } = loadModule(false);
  render(NOTICE);
  assert.ok(announcement.textContent.includes("Auto-renew is off"));
  assert.ok(!announcement.textContent.includes("<"), "markup must not reach the live region");
});

test("a grace deployment is not told it ends today", () => {
  const { render, banner } = loadModule(false);
  render({ ...NOTICE, days_remaining: 0, grace_days_remaining: 13 });
  assert.ok(banner.innerHTML.includes("service stops in 13 days"), banner.innerHTML);
  assert.ok(!banner.innerHTML.includes("Subscription ends today"), banner.innerHTML);
});

test("storage that throws leaves the banner expanded", () => {
  const { render, banner } = loadModule(true);
  render(NOTICE);
  const full = banner.innerHTML.slice(banner.innerHTML.indexOf('class="renewal-full"'));
  assert.ok(!full.slice(0, 80).includes("hidden"), full.slice(0, 120));
});

test("severity reaches the attribute the stylesheet keys on", () => {
  const { render, banner } = loadModule(false);
  render({ ...NOTICE, severity: "expired" });
  assert.equal(banner.dataset.severity, "expired");
});
"""


@pytest.fixture(scope="module")
def node() -> str:
    executable = shutil.which("node")
    if executable is None:  # pragma: no cover - environment dependent
        pytest.skip("node is not installed; the static contract in test_renewal_banner_ui.py holds")
    return executable


def test_the_banner_behaves_as_its_source_level_contract_claims(node, tmp_path):
    """One subprocess, every behavioural assertion, reported individually.

    Run as a single `node --test` invocation rather than a test-per-case so the
    suite pays one process start; the assertion failure message names the case.
    """
    harness = tmp_path / "renewal-banner.test.js"
    harness.write_text(_HARNESS, encoding="utf-8")

    result = subprocess.run(
        [node, "--test", str(harness)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "BANNER_JS": str(BANNER_JS)},
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_harness_would_catch_an_unescaped_headline(node, tmp_path):
    """Negative control: the harness must fail against a banner that does not
    escape. Without this, a harness that silently stopped executing the module —
    a changed export name, a stub that swallows — would look like proof."""
    broken = tmp_path / "broken.js"
    broken.write_text(
        BANNER_JS.read_text(encoding="utf-8").replace(
            "headline: escapeHtml(notice.headline),", "headline: String(notice.headline),"
        ),
        encoding="utf-8",
    )
    assert "String(notice.headline)" in broken.read_text(encoding="utf-8")

    harness = tmp_path / "control.test.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    result = subprocess.run(
        [node, "--test", str(harness)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "BANNER_JS": str(broken)},
        timeout=120,
        check=False,
    )
    assert result.returncode != 0, "the harness passed a banner that renders unescaped markup"
    assert "escaped" in (result.stdout + result.stderr)


def test_the_harness_would_catch_an_inverted_scheme_check(node, tmp_path):
    """The other mutation the static assertions could not see."""
    broken = tmp_path / "inverted.js"
    broken.write_text(
        BANNER_JS.read_text(encoding="utf-8").replace(
            'return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.href : "";',
            'return parsed.protocol === "https:" || parsed.protocol === "http:" ? "" : parsed.href;',
        ),
        encoding="utf-8",
    )
    harness = tmp_path / "control2.test.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    result = subprocess.run(
        [node, "--test", str(harness)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "BANNER_JS": str(broken)},
        timeout=120,
        check=False,
    )
    assert result.returncode != 0, "the harness passed a banner with an inverted scheme check"


def test_the_harness_names_the_module_the_repo_actually_ships():
    """A behavioural suite pointed at a copy proves nothing about the shipped
    asset. Cheap guard against the path drifting."""
    assert BANNER_JS.exists()
    assert BANNER_JS.parent.name == "admin_ui"
    assert "QueryGateRenewalBanner" in BANNER_JS.read_text(encoding="utf-8")
    assert json.dumps(str(BANNER_JS))  # path is representable; no surprises for the subprocess
