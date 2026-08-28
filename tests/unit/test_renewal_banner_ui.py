"""The renewal banner's accessibility contract, asserted on the shipped assets.

Item 216 makes accessibility part of the item rather than a follow-up, so these
are tests and not a review note. Static assertions over the real files, because
the properties are structural — a live region outside the accessibility tree at
the moment its content arrives is never announced, and no screenshot shows that.

**These assertions are deliberately non-invertible.** The first version of this
file asserted that tokens *appeared* (`escapeHtml(`, `"https:"`, two `.focus()`
calls) rather than that they did anything, and a review showed that inverting
`safeHref`'s ternary, replacing `escapeHtml` with the identity function, or
swapping the two focus targets all left it green while breaking exactly the
behaviour each docstring named. Where a static assertion cannot carry the
property, `tests/unit/test_renewal_banner_behaviour.py` executes the module.

Also asserts the banner is **shared** by both consoles rather than implemented
twice: two copies of a live region is two chances for one to stop announcing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

UI_ROOT = Path(__file__).resolve().parents[2] / "src" / "querygate"
ADMIN_HTML = UI_ROOT / "admin_ui" / "index.html"
ACCESS_HTML = UI_ROOT / "access_ui" / "index.html"
ADMIN_JS = UI_ROOT / "admin_ui" / "app.js"
ACCESS_JS = UI_ROOT / "access_ui" / "app.js"
BANNER_JS = UI_ROOT / "admin_ui" / "renewal-banner.js"
CSS = UI_ROOT / "admin_ui" / "app.css"
PAGES = (ADMIN_HTML, ACCESS_HTML)
APPS = (ADMIN_JS, ACCESS_JS)


def _without_comments(source: str) -> str:
    """Strip `//` and `/* */` so an assertion about *code* is neither satisfied
    nor broken by prose explaining that code. Naive on purpose — there is no
    `//` inside a string literal in these files, and the alternative is a JS
    parser."""
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return "\n".join(re.sub(r"//.*$", "", line) for line in source.splitlines())


# --- structure ----------------------------------------------------------------


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.parent.name)
def test_both_consoles_carry_the_banner(page: Path):
    """`access_ui` is where the analyst whose queries stop is looking, and they
    hold no admin scope. A warning only an admin sees is a warning the affected
    person first learns about from a 402."""
    assert 'id="renewal-banner"' in page.read_text(encoding="utf-8")


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.parent.name)
def test_the_live_region_holds_only_the_announcement_text(page: Path):
    """The visible banner is **outside** the live region, deliberately.

    `role="status"` carries an implicit `aria-atomic`, so any mutation inside it
    re-announces the whole region. With the collapse/expand controls in there,
    one keypress announced the entire banner and then the newly focused chip on
    top of it. A visually hidden region carrying one line of text says what
    changed, once.

    The region is also never `hidden`: `[hidden] { display: none !important }`
    on a `role="status"` element leaves a screen reader nothing to observe when
    the content arrives.
    """
    markup = page.read_text(encoding="utf-8")

    region = re.search(r'<p id="renewal-announcement"[^>]*>', markup)
    assert region is not None
    assert 'role="status"' in region.group(0)
    assert "sr-only" in region.group(0)
    assert "hidden" not in region.group(0)

    banner = re.search(r'<div id="renewal-banner"[^>]*>', markup)
    assert banner is not None
    assert "hidden" in banner.group(0)
    assert "role=" not in banner.group(0), "the visible banner must not be a second live region"

    # And the banner is a sibling, not a child: the region closes first.
    region_at = markup.index('id="renewal-announcement"')
    banner_at = markup.index('id="renewal-banner"')
    assert region_at < markup.index("</p>", region_at) < banner_at


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.parent.name)
def test_both_consoles_load_the_one_shared_implementation(page: Path):
    markup = page.read_text(encoding="utf-8")
    assert '<script src="/admin/renewal-banner.js" defer></script>' in markup


def test_the_banner_is_implemented_once():
    """Neither app.js may grow its own renderer."""
    for app_js in APPS:
        source = app_js.read_text(encoding="utf-8")
        assert "QueryGateRenewalBanner.render" in source
        assert "renewal-headline" not in source, app_js
        assert "renewal-chip" not in source, app_js


# --- when the consoles render it ----------------------------------------------


@pytest.mark.parametrize("app_js", APPS, ids=lambda p: p.parent.name)
def test_the_banner_is_never_rendered_while_the_auth_dialog_is_open(app_js: Path):
    """The highest-severity defect this file exists to prevent.

    A modal `<dialog>` makes every node outside it `inert`, and an inert subtree
    is not exposed to the accessibility tree. Rendering inside `loadAccess()` —
    which both token and local-password sign-in await *before* `close()` — meant
    a screen-reader user never heard the expiry warning on any path where the
    dialog was actually shown. Closing the dialog afterwards is not a text
    change; nothing re-announces.

    Asserted as ordering: every `refreshRenewalBanner()` call must follow a
    `close()` or a resume that never opened the dialog, and `loadAccess` must
    not contain one at all.
    """
    code = _without_comments(app_js.read_text(encoding="utf-8"))

    load_access = code[code.index("async function loadAccess()") :]
    load_access = load_access[: load_access.index("\n  function ")]
    assert "refreshRenewalBanner" not in load_access, "renders while the modal may still be open"

    for match in re.finditer(r"void refreshRenewalBanner\(\);", code):
        preceding = code[: match.start()]
        anchor = max(
            preceding.rfind('$("#auth-dialog").close();'),
            preceding.rfind("if (await resumeSession()) {"),
        )
        assert anchor != -1, "a render that follows neither a dialog close nor a session resume"
        # Nothing may reopen the dialog between the anchor and the render.
        assert "showModal()" not in code[anchor : match.start()]


@pytest.mark.parametrize("app_js", APPS, ids=lambda p: p.parent.name)
def test_signing_out_clears_the_countdown(app_js: Path):
    """A `role="status"` region left asserting a previous session's expiry has
    no visual "stale" cue, so it reads as current — and if the next principal's
    fetch fails, that stale text is what stays on screen."""
    code = _without_comments(app_js.read_text(encoding="utf-8"))
    disconnect = code[code.index("function disconnect()") :]
    disconnect = disconnect[: disconnect.index("\n  }")]
    assert "QueryGateRenewalBanner.render(null)" in disconnect


@pytest.mark.parametrize("app_js", APPS, ids=lambda p: p.parent.name)
def test_a_failed_fetch_clears_the_banner_rather_than_leaving_it(app_js: Path):
    """Swallowing the error is right — no countdown beats a broken workspace —
    but swallowing it *without clearing* preserves the last good answer and
    presents it as current."""
    code = _without_comments(app_js.read_text(encoding="utf-8"))
    body = code[code.index("async function refreshRenewalBanner()") :]
    body = body[: body.index("\n  }")]
    catch = body[body.index("catch") :]
    assert "QueryGateRenewalBanner.render(null)" in catch


def test_the_banner_is_cleared_rather_than_left_stale():
    """`render(null)` must empty the markup, not merely hide it.

    A hidden-but-populated banner is one `hidden = false` away from re-asserting
    a previous session's countdown, and the announcement region would still hold
    its text for a screen reader to find.
    """
    code = _without_comments(BANNER_JS.read_text(encoding="utf-8"))
    empty = code.split("if (!notice) {")[1].split("return;")[0]
    assert "banner.hidden = true" in empty
    assert 'banner.innerHTML = ""' in empty
    assert 'announcement.textContent = ""' in empty


# --- the disclosure -----------------------------------------------------------


def test_collapsing_keeps_the_countdown_visible():
    """ "Dismissible without losing the information" is the requirement.

    Collapsing hides the detail and leaves a chip that still names the day
    count, so the fact never leaves the page — a plain hide would.
    """
    code = _without_comments(BANNER_JS.read_text(encoding="utf-8"))
    assert "Subscription ends ${dayLabel(days)}" in code
    assert "data-renewal-expand" in code


def test_collapsing_is_session_scoped_so_the_banner_is_actually_persistent():
    """`localStorage` would make one click a permanent opt-out of knowing the
    subscription is ending."""
    code = _without_comments(BANNER_JS.read_text(encoding="utf-8"))
    assert "sessionStorage" in code
    assert "localStorage" not in code


def test_storage_being_unavailable_leaves_the_banner_expanded():
    """Private mode, or storage disabled by policy. A hidden warning is a worse
    failure than a repeated one, so the fallback must be `false`.

    Asserts the success path too: `getItem(...) !== "1"` would make every banner
    render collapsed by default while `catch` and `return false` both still
    appear in the function.
    """
    code = _without_comments(BANNER_JS.read_text(encoding="utf-8"))
    body = code.split("function isCollapsed()")[1].split("function setCollapsed")[0]
    assert 'getItem(COLLAPSE_KEY) === "1"' in body
    catch = body[body.index("catch") :]
    assert "return false" in catch


def test_focus_moves_to_the_control_that_replaced_the_one_that_vanished():
    """Both toggles remove the focused element from the tab order. Without an
    explicit move, a keyboard user is dropped back to the document top.

    Asserts the *target*, not a count. The first version asserted
    `count(".focus()") == 2`, which passes just as happily when each handler
    focuses the element it just hid — the precise failure this names — and fails
    spuriously the first time an unrelated third `.focus()` is added.
    """
    code = _without_comments(BANNER_JS.read_text(encoding="utf-8"))
    assert "(expanded ? collapse : expand).focus()" in code
    assert "panel.hidden = !expanded" in code
    assert "expand.hidden = expanded" in code


def test_the_disclosure_exposes_its_state_programmatically():
    """A collapsed panel a screen reader cannot detect is not a disclosure.

    `#menu-button` in the same console already does this correctly, so it is
    consistency with a local pattern rather than a preference.
    """
    code = _without_comments(BANNER_JS.read_text(encoding="utf-8"))
    assert 'aria-controls="renewal-detail-panel"' in code
    assert 'id="renewal-detail-panel"' in code
    assert 'setAttribute("aria-expanded", String(expanded))' in code


def test_the_collapsed_chip_does_not_claim_a_grace_deployment_ends_today():
    """In grace the paid term has already ended, so `days_remaining` is 0.

    A chip rendered from that alone tells a deployment with thirteen days of
    grace left that it "ends today" — wrong in the alarming direction, and
    contradicting the expanded headline directly above it.
    """
    code = _without_comments(BANNER_JS.read_text(encoding="utf-8"))
    assert "grace_days_remaining" in code
    assert "Subscription lapsed" in code


# --- safe rendering -----------------------------------------------------------


def test_the_markup_template_never_names_a_notice_field_directly():
    """The structural property that makes escaping auditable.

    `notice.x` interpolated into markup is safe only if it happens to sit inside
    `escapeHtml(...)`, and checking that with a regex fails on nesting — the
    first version of this test passed a `formatDate(notice.expires_at)` escaped
    one level up, and would equally have passed one that was not.

    So the code escapes everything into a `text` object first and the template
    interpolates only from it. Then "is every value escaped?" is answerable by
    reading the six lines that build `text`.
    """
    code = _without_comments(BANNER_JS.read_text(encoding="utf-8"))
    template = code.split("banner.innerHTML = `")[1].split("`;")[0]
    assert "notice." not in template, template


def test_every_field_of_the_notice_is_escaped_before_it_reaches_the_markup():
    code = _without_comments(BANNER_JS.read_text(encoding="utf-8"))
    block = code.split("const text = {")[1].split("};")[0]
    values = [line.split(":", 1)[1] for line in block.splitlines() if ":" in line]
    assert values
    assert all("escapeHtml(" in value for value in values), block


def test_the_renewal_link_is_scheme_checked_not_merely_escaped():
    """Escaping stops `"` closing the attribute; it does not stop `javascript:`
    being the whole value. A renewal link is exactly what someone clicks without
    reading it.

    Asserts the whole comparison rather than three substrings: the first version
    checked that `"https:"`, `"http:"` and `return ""` each appeared somewhere
    in the function, which an **inverted ternary** satisfies while rendering
    `javascript:` URLs into the href. The behavioural proof is in
    `test_renewal_banner_behaviour.py`; this keeps the source honest.
    """
    code = _without_comments(BANNER_JS.read_text(encoding="utf-8"))
    assert "safeHref(notice.renewal_url)" in code
    assert (
        'return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.href : "";'
        in code
    )


def test_the_new_tab_is_announced():
    """Activating the link lands the reader in a new tab with Back doing
    nothing. `.sr-only` already exists in the stylesheet."""
    code = _without_comments(BANNER_JS.read_text(encoding="utf-8"))
    assert 'target="_blank"' in code
    assert '<span class="sr-only"> (opens in a new tab)</span>' in code


# --- presentation -------------------------------------------------------------


def test_severity_is_never_the_only_signal():
    """Colour alone fails for a colour-blind operator and in a monochrome
    screenshot. The headline says what is happening in words; severity only
    changes how loud it looks."""
    css = CSS.read_text(encoding="utf-8")
    assert '.renewal-banner[data-severity="critical"]' in css
    assert '.renewal-banner[data-severity="expired"]' in css
    code = _without_comments(BANNER_JS.read_text(encoding="utf-8"))
    # The attribute the selectors key on must actually be set.
    assert "banner.dataset.severity = notice.severity" in code
    assert "text.headline" in code


def test_the_banner_reuses_the_palette_the_console_already_meets():
    """A new colour pair here would be a new contrast bar to defend. These are
    the tokens `.banner` and the status chips already use, so the banner meets
    whatever the rest of the console meets, by construction.

    (`ui-a11y-reviewer` computed the composited ratios: 9.24:1 for the warning
    palette, 6.32:1 escalated, 13.28:1 for `--ink` detail, 7.21:1 for `--muted`
    dates. `--faint` would fail at 4.16:1 — hence the guard below.)
    """
    css = CSS.read_text(encoding="utf-8")
    block = css.split(".renewal-banner { margin: 22px")[1].split("\n")[0]
    assert "var(--warning)" in block
    assert "var(--warning-soft)" in block
    assert "#" not in block, "a literal colour here escapes the shared palette"
    escalated = css.split('.renewal-banner[data-severity="critical"]')[1].split("\n")[0]
    assert "var(--bad)" in escalated


def test_no_renewal_rule_reaches_for_the_one_token_that_would_fail_contrast():
    """`--faint` computes to 4.16:1 on the warning tint and 4.47:1 on the bad
    tint — both under AA. Nothing in the banner uses it today, and the global
    `small { color: var(--faint) }` means the first `<small>` added inside the
    banner would fail immediately. Cheap tripwire for a latent trap."""
    css = CSS.read_text(encoding="utf-8")
    renewal_rules = [line for line in css.splitlines() if line.startswith(".renewal")]
    assert renewal_rules
    assert not [line for line in renewal_rules if "var(--faint)" in line]
    code = _without_comments(BANNER_JS.read_text(encoding="utf-8"))
    assert "<small" not in code


def test_the_banner_gets_the_same_narrow_viewport_inset_as_its_neighbour():
    """`#global-banner` sits directly beneath it and `.view` content beside it;
    all three must share one inset or the two banners misalign by 8px at the
    width where alignment is most visible."""
    css = CSS.read_text(encoding="utf-8")
    narrow = css.split("@media (max-width: 680px)")[1]
    assert ".banner, .renewal-banner { margin-inline: 16px; }" in narrow


def test_the_collapse_control_meets_the_minimum_target_size():
    """`.text-button` computes to 12px x 1.5 = 18px, under WCAG 2.2 SC 2.5.8's
    24x24. Rescued by the spacing exception at the current link width, which is
    a margin nobody should be relying on."""
    css = CSS.read_text(encoding="utf-8")
    assert ".renewal-actions .text-button { min-height: 24px; }" in css
