/**
 * The renewal countdown banner (TODO.md item 216), shared by admin_ui and
 * access_ui.
 *
 * Shared rather than duplicated because the parts that must not diverge are the
 * accessibility contract and the escalation threshold. Two copies of a live
 * region is two chances for one of them to stop announcing, and two copies of
 * "is this critical yet" is a banner that turns red in one UI and not the other.
 *
 * Rendering only — the caller does the fetch, because the two UIs authenticate
 * differently (bearer token vs. SSO cookie plus CSRF header) and neither should
 * hand its credential to a shared module.
 *
 * Served from /admin/, which access_ui already loads its stylesheet from.
 */
(() => {
  "use strict";

  const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  }[character]));

  // Session-scoped, not localStorage: "persistent banner" is the requirement,
  // so a new tab or a reload shows it expanded again. Collapsing is a
  // this-tab-right-now convenience, not a permanent opt-out of knowing the
  // subscription is ending.
  const COLLAPSE_KEY = "querygate.renewal.collapsed";

  function isCollapsed() {
    try {
      return window.sessionStorage.getItem(COLLAPSE_KEY) === "1";
    } catch (error) {
      // Private mode, or storage disabled by policy. Default to expanded: the
      // failure mode of a hidden warning is worse than of a repeated one.
      return false;
    }
  }

  function setCollapsed(collapsed) {
    try {
      if (collapsed) window.sessionStorage.setItem(COLLAPSE_KEY, "1");
      else window.sessionStorage.removeItem(COLLAPSE_KEY);
    } catch (error) {
      /* not being able to remember the preference is not an error worth showing */
    }
  }

  function formatDate(iso) {
    // The exact date, not a relative phrase: "in 5 days" alone is ambiguous
    // across timezones and unusable in a calendar reminder, which is the first
    // thing an operator does with this.
    const parsed = new Date(iso);
    if (Number.isNaN(parsed.getTime())) return String(iso);
    return parsed.toISOString().slice(0, 10);
  }

  function dayLabel(days) {
    if (days === 0) return "today";
    return `${days} day${days === 1 ? "" : "s"}`;
  }

  // Escaping stops `"` closing the attribute; it does not stop `javascript:`
  // being the whole value. This URL arrives inside a document our own control
  // plane signs, so this is defence in depth rather than a boundary — but a
  // renewal link is exactly the thing a person clicks without reading, and the
  // check costs one comparison.
  function safeHref(url) {
    try {
      const parsed = new URL(String(url), window.location.origin);
      return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.href : "";
    } catch (error) {
      return "";
    }
  }

  /**
   * Render `notice` (the `notice` field of GET /api/v1/subscription) into the
   * page. `null` hides the banner. Idempotent — safe to call on every refresh.
   */
  function render(notice) {
    const banner = document.querySelector("#renewal-banner");
    const announcement = document.querySelector("#renewal-announcement");
    if (!banner) return;

    if (!notice) {
      banner.hidden = true;
      banner.removeAttribute("data-severity");
      banner.innerHTML = "";
      if (announcement) announcement.textContent = "";
      return;
    }

    const collapsed = isCollapsed();
    const days = Number(notice.days_remaining) || 0;
    const graceDays = notice.grace_days_remaining;
    banner.dataset.severity = notice.severity;
    banner.hidden = false;

    // In grace the paid term has already ended, so `days_remaining` is 0 and a
    // chip rendered from it alone tells a deployment with thirteen days of
    // grace left that it "ends today" — wrong, in the alarming direction, and
    // disagreeing with the expanded headline directly above it.
    let summary;
    if (notice.severity === "expired") {
      summary = "Subscription expired";
    } else if (notice.grace_expires_at && days === 0 && typeof graceDays === "number") {
      summary = `Subscription lapsed — service stops in ${dayLabel(graceDays)}`;
    } else {
      summary = `Subscription ends ${dayLabel(days)}`;
    }

    const dates = [`Term ends ${formatDate(notice.expires_at)}`];
    if (notice.grace_expires_at) dates.push(`grace ends ${formatDate(notice.grace_expires_at)}`);

    // Everything the notice supplies is escaped here, in one block, and the
    // markup below interpolates only from `text`. Keeping the template free of
    // `notice.` is what makes "is every value escaped?" answerable by looking
    // at eight lines instead of by reading the markup carefully — and it is
    // asserted in tests/unit/test_renewal_banner_ui.py rather than trusted.
    const text = {
      headline: escapeHtml(notice.headline),
      detail: escapeHtml(notice.detail),
      dates: escapeHtml(dates.join(" · ")),
      summary: escapeHtml(summary),
      href: escapeHtml(safeHref(notice.renewal_url)),
    };
    const link = text.href
      ? `<a class="renewal-link" href="${text.href}" rel="noopener noreferrer" target="_blank">Renew this subscription<span class="sr-only"> (opens in a new tab)</span></a>`
      : "";

    // Both the expanded panel and the collapsed chip are always in the DOM, so
    // collapsing hides detail rather than removing the fact — the chip still
    // carries the countdown and re-expands from the keyboard.
    banner.innerHTML = `
      <div class="renewal-full" id="renewal-detail-panel"${collapsed ? " hidden" : ""}>
        <p class="renewal-headline">${text.headline}</p>
        <p class="renewal-detail">${text.detail}</p>
        <p class="renewal-dates">${text.dates}</p>
        <div class="renewal-actions">
          ${link}
          <button class="text-button" type="button" data-renewal-collapse
                  aria-expanded="true" aria-controls="renewal-detail-panel">Collapse this notice</button>
        </div>
      </div>
      <button class="renewal-chip" type="button" data-renewal-expand
              aria-expanded="false" aria-controls="renewal-detail-panel"${collapsed ? "" : " hidden"}>
        ${text.summary} — show details
      </button>`;

    // The announcement is a *separate*, visually hidden live region, and the
    // visible banner is outside it. `role="status"` carries an implicit
    // `aria-atomic`, so any mutation inside it re-announces the whole region —
    // with the controls in there, one collapse keypress announced the entire
    // banner and then the focused chip on top of it. Setting one line of text
    // here says exactly what changed, once.
    if (announcement) announcement.textContent = `${notice.headline} ${summary}.`;

    const panel = banner.querySelector(".renewal-full");
    const collapse = banner.querySelector("[data-renewal-collapse]");
    const expand = banner.querySelector("[data-renewal-expand]");

    const setExpanded = (expanded) => {
      setCollapsed(!expanded);
      panel.hidden = !expanded;
      expand.hidden = expanded;
      collapse.setAttribute("aria-expanded", String(expanded));
      expand.setAttribute("aria-expanded", String(expanded));
      // Focus follows the control that replaced the one just removed from the
      // tab order, so a keyboard user is not dropped back to the document top.
      (expanded ? collapse : expand).focus();
    };

    collapse.addEventListener("click", () => setExpanded(false));
    expand.addEventListener("click", () => setExpanded(true));
  }

  window.QueryGateRenewalBanner = { render };
})();
