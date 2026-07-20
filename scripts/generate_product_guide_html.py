#!/usr/bin/env python3
"""Renders docs/PRODUCT_GUIDE.md into a browsable docs/product-guide.html.

Regenerate after editing docs/PRODUCT_GUIDE.md:
    poetry run python scripts/generate_product_guide_html.py
"""
import html
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "docs" / "PRODUCT_GUIDE.md"
OUT = REPO_ROOT / "docs" / "product-guide.html"

# Brand mark: same badge (dark square, orange semicolon) and wordmark accent
# used by src/querygate/admin_ui/favicon.svg + logo-wordmark.svg. The
# semicolon is QueryGate's actual brand mark, not a placeholder.
BRAND_ORANGE = "#FF914D"
BRAND_DARK = "#101216"


def slug(text: str) -> str:
    s = text.lower()
    s = re.sub(r"[^a-z0-9 -]", "", s)
    return s.replace(" ", "-")


_used_ids: set[str] = set()


def uniq_id(base: str) -> str:
    if base not in _used_ids:
        _used_ids.add(base)
        return base
    i = 2
    while f"{base}-{i}" in _used_ids:
        i += 1
    _used_ids.add(f"{base}-{i}")
    return f"{base}-{i}"


def inline(text: str) -> str:
    code_spans: list[str] = []

    def stash_code(m: re.Match) -> str:
        code_spans.append(html.escape(m.group(1), quote=False))
        return f"\x00CODE{len(code_spans) - 1}\x00"

    text = re.sub(r"`([^`]+)`", stash_code, text)
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"<em>\1</em>", text)

    def link_repl(m: re.Match) -> str:
        label, href = m.group(1), m.group(2)
        target = ' target="_blank" rel="noopener"' if href.startswith("http") else ""
        return f'<a href="{href}"{target}>{label}</a>'

    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link_repl, text)

    def restore(m: re.Match) -> str:
        return f"<code>{code_spans[int(m.group(1))]}</code>"

    return re.sub(r"\x00CODE(\d+)\x00", restore, text)


def render_blocks(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    i, n = 0, len(lines)
    para_buf: list[str] = []

    def flush_para(buf: list[str]) -> None:
        if buf:
            joined = " ".join(l.strip() for l in buf if l.strip())
            if joined:
                out.append(f"<p>{inline(joined)}</p>")
            buf.clear()

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if stripped == "":
            flush_para(para_buf)
            i += 1
            continue

        if stripped.startswith("```"):
            flush_para(para_buf)
            i += 1
            code_lines = []
            while i < n and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1
            code_txt = html.escape("\n".join(code_lines), quote=False)
            out.append(f'<div class="code-block"><pre><code>{code_txt}</code></pre></div>')
            continue

        if stripped.startswith("#### "):
            flush_para(para_buf)
            title = stripped[5:].strip()
            out.append(f'<h4 id="{uniq_id(slug(title))}">{inline(title)}</h4>')
            i += 1
            continue

        if stripped.startswith("### "):
            flush_para(para_buf)
            title = stripped[4:].strip()
            out.append(f'<h3 id="{uniq_id(slug(title))}">{inline(title)}</h3>')
            i += 1
            continue

        if stripped.startswith("> "):
            flush_para(para_buf)
            quote_lines = []
            while i < n and lines[i].strip().startswith(">"):
                quote_lines.append(lines[i].strip()[1:].strip())
                i += 1
            joined = " ".join(x for x in quote_lines if x)
            out.append(f"<blockquote>{inline(joined)}</blockquote>")
            continue

        if stripped.startswith("|"):
            flush_para(para_buf)
            table_lines = []
            while i < n and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].strip())
                i += 1
            rows = [[c.strip() for c in tl.strip("|").split("|")] for tl in table_lines]
            header = rows[0]
            body_rows = (
                rows[2:] if len(rows) > 1 and re.match(r"^-+$", rows[1][0].strip()) else rows[1:]
            )
            th = "".join(f"<th>{inline(c)}</th>" for c in header)
            trs = [
                "<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in body_rows
            ]
            out.append(
                f'<div class="table-wrap"><table><thead><tr>{th}</tr></thead><tbody>{"".join(trs)}</tbody></table></div>'
            )
            continue

        m_bullet = re.match(r"^([-*])\s+(.*)$", line)
        m_num = re.match(r"^\d+\.\s+(.*)$", line)
        if m_bullet or m_num:
            flush_para(para_buf)
            is_ol = bool(m_num)
            items = []
            cur_item = m_num.group(1) if m_num else m_bullet.group(2)
            i += 1
            while i < n:
                nxt = lines[i]
                if nxt.strip() == "":
                    nxt_is_cont = i + 1 < n and (
                        re.match(r"^([-*])\s+", lines[i + 1])
                        or re.match(r"^\d+\.\s+", lines[i + 1])
                        or lines[i + 1].startswith("  ")
                    )
                    if nxt_is_cont:
                        i += 1
                        continue
                    break
                nb = re.match(r"^([-*])\s+(.*)$", nxt)
                nn = re.match(r"^\d+\.\s+(.*)$", nxt)
                if nb or nn:
                    items.append(cur_item)
                    cur_item = nn.group(1) if nn else nb.group(2)
                    i += 1
                elif nxt.startswith("  ") and nxt.strip():
                    cur_item += " " + nxt.strip()
                    i += 1
                else:
                    break
            items.append(cur_item)
            tag = "ol" if is_ol else "ul"
            lis = "".join(f"<li>{inline(it)}</li>" for it in items)
            out.append(f"<{tag}>{lis}</{tag}>")
            continue

        para_buf.append(line)
        i += 1

    flush_para(para_buf)
    return "\n".join(out)


def split_on_hr(md: str) -> list[str]:
    lines = md.split("\n")
    chunks: list[str] = []
    current: list[str] = []
    for line in lines:
        if line.strip() == "---":
            chunks.append("\n".join(current))
            current = []
        else:
            current.append(line)
    chunks.append("\n".join(current))
    return chunks


def split_sections(rest: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    current_title = None
    current_body: list[str] = []
    for line in rest.split("\n"):
        if line.startswith("## ") and not line.startswith("### "):
            if current_title is not None:
                sections.append((current_title, "\n".join(current_body)))
            current_title = line[3:].strip()
            current_body = []
        else:
            current_body.append(line)
    if current_title is not None:
        sections.append((current_title, "\n".join(current_body)))
    return sections


PAGE_TEMPLATE = """<title>QueryGate Product Guide</title>
<style>
:root {{
  --bg: #f7f5f1; --surface: #ffffff; --surface-2: #f1ede6; --sidebar-bg: #efe9e0;
  --ink: #1c1a17; --muted: #5b564c; --faint: #857f73; --line: #e2dcd0; --line-strong: #c9c1b1;
  --accent: #c2621f; --accent-strong: #a8531a; --accent-ink: #fff8f2;
  --accent-soft: rgba(194, 98, 31, .10); --good: #1f8f66; --good-soft: rgba(31, 143, 102, .10);
  --warning: #93650f; --warning-soft: rgba(169, 118, 27, .12); --code-bg: #f1ede6;
  --sidebar-width: 288px;
  --font-serif: Georgia, "Times New Roman", serif;
  --font-body: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, sans-serif;
  --font-mono: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  color-scheme: light;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #101216; --surface: #181b20; --surface-2: #20242b; --sidebar-bg: #0b0d10;
    --ink: #f2f3f5; --muted: #aeb6c2; --faint: #7f8997; --line: #2b3038; --line-strong: #444b56;
    --accent: #ff9a5c; --accent-strong: #ff8240; --accent-ink: #1b0e06;
    --accent-soft: rgba(255, 154, 92, .13); --good: #68d3a6; --good-soft: rgba(104, 211, 166, .12);
    --warning: #f4c66f; --warning-soft: rgba(244, 198, 111, .12); --code-bg: #20242b;
    color-scheme: dark;
  }}
}}
:root[data-theme="dark"] {{
  --bg: #101216; --surface: #181b20; --surface-2: #20242b; --sidebar-bg: #0b0d10;
  --ink: #f2f3f5; --muted: #aeb6c2; --faint: #7f8997; --line: #2b3038; --line-strong: #444b56;
  --accent: #ff9a5c; --accent-strong: #ff8240; --accent-ink: #1b0e06;
  --accent-soft: rgba(255, 154, 92, .13); --good: #68d3a6; --good-soft: rgba(104, 211, 166, .12);
  --warning: #f4c66f; --warning-soft: rgba(244, 198, 111, .12); --code-bg: #20242b;
  color-scheme: dark;
}}
:root[data-theme="light"] {{
  --bg: #f7f5f1; --surface: #ffffff; --surface-2: #f1ede6; --sidebar-bg: #efe9e0;
  --ink: #1c1a17; --muted: #5b564c; --faint: #857f73; --line: #e2dcd0; --line-strong: #c9c1b1;
  --accent: #c2621f; --accent-strong: #a8531a; --accent-ink: #fff8f2;
  --accent-soft: rgba(194, 98, 31, .10); --good: #1f8f66; --good-soft: rgba(31, 143, 102, .10);
  --warning: #93650f; --warning-soft: rgba(169, 118, 27, .12); --code-bg: #f1ede6;
  color-scheme: light;
}}

* {{ box-sizing: border-box; }}
html {{ background: var(--bg); }}
@media (prefers-reduced-motion: no-preference) {{ html {{ scroll-behavior: smooth; }} }}
body {{ margin: 0; background: var(--bg); color: var(--ink); font: 16px/1.65 var(--font-body); -webkit-font-smoothing: antialiased; }}
a {{ color: var(--accent); text-decoration-color: color-mix(in srgb, var(--accent) 45%, transparent); text-underline-offset: 2px; }}
a:hover {{ text-decoration-color: var(--accent); }}
:focus-visible {{ outline: 3px solid var(--accent); outline-offset: 2px; }}
code {{ font-family: var(--font-mono); font-size: .88em; background: var(--code-bg); border: 1px solid var(--line); border-radius: 4px; padding: .1em .35em; color: var(--accent-strong); }}
h1, h2, h3, h4 {{ font-family: var(--font-serif); font-weight: 500; letter-spacing: -.01em; text-wrap: balance; color: var(--ink); }}
p, li {{ text-wrap: pretty; }}

.app-shell {{ min-height: 100vh; }}

.sidebar {{ position: fixed; inset: 0 auto 0 0; z-index: 40; width: var(--sidebar-width); height: 100vh; display: flex; flex-direction: column; background: var(--sidebar-bg); border-right: 1px solid var(--line); }}
.brand {{ display: flex; align-items: center; gap: 12px; padding: 22px 20px; border-bottom: 1px solid var(--line); }}
.brand-mark {{ flex: 0 0 auto; width: 34px; height: 34px; border-radius: 8px; overflow: hidden; }}
.brand-mark svg {{ display: block; width: 100%; height: 100%; }}
.brand-text {{ display: flex; flex-direction: column; gap: 2px; min-width: 0; }}
.brand-text .wordmark {{ font: 500 17px/1.1 var(--font-serif); color: var(--ink); letter-spacing: -.01em; }}
.brand-text .wordmark .semi {{ color: {brand_orange}; }}
.brand-text small {{ font: 10px/1.4 var(--font-mono); letter-spacing: .09em; text-transform: uppercase; color: var(--faint); }}

.progress-chip {{ margin: 16px 20px 0; padding: 10px 12px; border: 1px solid var(--line); border-radius: 7px; background: var(--surface); display: flex; align-items: center; gap: 9px; }}
.progress-dot {{ width: 7px; height: 7px; border-radius: 50%; background: var(--good); box-shadow: 0 0 0 4px var(--good-soft); flex: 0 0 auto; }}
.progress-chip span {{ font: 11px/1.4 var(--font-mono); color: var(--muted); }}
.progress-chip strong {{ font-variant-numeric: tabular-nums; color: var(--ink); }}

nav.toc {{ flex: 1; overflow-y: auto; padding: 16px 12px; display: flex; flex-direction: column; gap: 2px; }}
nav.toc ul {{ list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }}
.nav-item {{ display: flex; align-items: center; gap: 10px; min-height: 38px; padding: 0 12px; border-radius: 6px; color: var(--muted); text-decoration: none; font-size: 13.5px; border: 1px solid transparent; }}
.nav-item:hover {{ background: var(--surface); color: var(--ink); }}
.nav-item.active {{ background: var(--surface); border-color: var(--line); color: var(--ink); font-weight: 600; }}
.nav-dot {{ width: 5px; height: 5px; border-radius: 50%; background: var(--line-strong); flex: 0 0 auto; }}
.nav-item.active .nav-dot {{ background: var(--accent); }}

.sidebar-foot {{ padding: 16px 20px 20px; border-top: 1px solid var(--line); }}
.sidebar-foot p {{ margin: 0; font-size: 11.5px; line-height: 1.5; color: var(--faint); }}

.content {{ margin-left: var(--sidebar-width); }}
.content-inner {{ max-width: 760px; margin: 0 auto; padding: 56px 40px 120px; }}

.masthead {{ margin-bottom: 40px; }}
.eyebrow {{ display: block; margin-bottom: 12px; font: 11px/1 var(--font-mono); letter-spacing: .1em; text-transform: uppercase; color: var(--accent); }}
.masthead h1 {{ margin: 0 0 18px; font-size: clamp(2rem, 4vw, 2.6rem); line-height: 1.1; }}
.masthead .lede p {{ margin: 0 0 12px; color: var(--muted); font-size: 15.5px; }}
.masthead .lede p:last-child {{ margin-bottom: 0; }}
.masthead .lede strong {{ color: var(--ink); }}
.style-rules {{ margin-top: 18px; padding: 16px 18px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); }}
.style-rules ul {{ margin: 8px 0 0; padding-left: 20px; color: var(--muted); font-size: 13.5px; line-height: 1.6; }}
.style-rules p {{ margin: 0; font-size: 12px; font-family: var(--font-mono); letter-spacing: .04em; text-transform: uppercase; color: var(--faint); }}

.maint-panel {{ margin-bottom: 48px; padding: 20px 22px; border-radius: 10px; background: var(--accent-soft); border: 1px solid color-mix(in srgb, var(--accent) 35%, var(--line)); }}
.maint-panel .eyebrow {{ margin-bottom: 10px; }}
.maint-panel p {{ margin: 0 0 10px; font-size: 14px; color: var(--ink); }}
.maint-panel p:last-child {{ margin-bottom: 0; }}
.maint-panel ul {{ margin: 8px 0; padding-left: 20px; font-size: 14px; }}

.doc-section {{ padding-top: 44px; margin-top: 4px; border-top: 1px solid var(--line); }}
.doc-section:first-of-type {{ border-top: none; }}
.doc-section h2 {{ font-size: clamp(1.5rem, 2.6vw, 1.85rem); margin: 0 0 20px; }}
.doc-section h3 {{ font-size: 1.15rem; margin: 32px 0 12px; }}
.doc-section h4 {{ font-size: .95rem; margin: 22px 0 8px; font-family: var(--font-body); font-weight: 700; }}
.doc-section p {{ margin: 0 0 14px; color: var(--ink); }}
.doc-section ul, .doc-section ol {{ margin: 0 0 16px; padding-left: 22px; }}
.doc-section li {{ margin-bottom: 8px; }}
.doc-section li:last-child {{ margin-bottom: 0; }}

blockquote {{ margin: 0 0 16px; padding: 4px 18px; border-left: 3px solid var(--accent); color: var(--muted); font-style: italic; font-size: 15px; }}

.code-block {{ margin: 0 0 16px; overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; background: var(--code-bg); }}
.code-block pre {{ margin: 0; padding: 14px 16px; }}
.code-block code {{ background: none; border: none; padding: 0; color: var(--ink); font-size: 13px; line-height: 1.6; white-space: pre; }}

.table-wrap {{ overflow-x: auto; margin: 0 0 16px; border: 1px solid var(--line); border-radius: 8px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13.5px; }}
th, td {{ text-align: left; padding: 9px 14px; border-bottom: 1px solid var(--line); font-variant-numeric: tabular-nums; white-space: nowrap; }}
td:last-child, th:last-child {{ white-space: normal; }}
thead th {{ background: var(--surface-2); font-weight: 700; color: var(--ink); }}
tbody tr:last-child td {{ border-bottom: none; }}

.content-inner > footer {{ margin-top: 56px; padding-top: 24px; border-top: 1px solid var(--line); }}
.content-inner > footer p {{ margin: 0; font-size: 12.5px; color: var(--faint); }}

@media (max-width: 900px) {{
  .sidebar {{ position: static; width: 100%; height: auto; flex-direction: row; align-items: center; overflow-x: auto; border-right: none; border-bottom: 1px solid var(--line); }}
  .brand {{ border-bottom: none; border-right: 1px solid var(--line); flex: 0 0 auto; }}
  .progress-chip {{ display: none; }}
  nav.toc {{ flex-direction: row; overflow-x: auto; padding: 10px 12px; }}
  nav.toc ul {{ flex-direction: row; }}
  .nav-item {{ white-space: nowrap; }}
  .sidebar-foot {{ display: none; }}
  .content {{ margin-left: 0; }}
  .content-inner {{ padding: 36px 22px 80px; }}
}}
</style>

<div class="app-shell">
  <aside class="sidebar">
    <div class="brand">
      <div class="brand-mark" aria-hidden="true">
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img" aria-label="QueryGate">
          <rect width="64" height="64" rx="12" fill="{brand_dark}"/>
          <text x="20" y="46" font-size="46" fill="{brand_orange}" font-family="Georgia, 'Times New Roman', serif" font-weight="500">;</text>
        </svg>
      </div>
      <div class="brand-text">
        <span class="wordmark">QueryGate<span class="semi">;</span></span>
        <small>Product Guide</small>
      </div>
    </div>
    <div class="progress-chip">
      <span class="progress-dot"></span>
      <span><strong>{section_count}</strong> / {section_count} sections written — kept current after every task</span>
    </div>
    <nav class="toc" aria-label="Guide sections">
      <ul>
        {nav_html}
      </ul>
    </nav>
    <div class="sidebar-foot">
      <p>Source of truth: <code>docs/PRODUCT_GUIDE.md</code> in the repo. This page is a generated, browsable copy — regenerate with <code>scripts/generate_product_guide_html.py</code>.</p>
    </div>
  </aside>

  <div class="content">
    <div class="content-inner">
      <header class="masthead">
        <span class="eyebrow">Internal · Human-facing · Also used for positioning</span>
        <h1>{doc_title}</h1>
        <div class="lede">
          {intro_html}
        </div>
      </header>

      <div class="maint-panel">
        <span class="eyebrow">Maintenance protocol</span>
        {maint_html}
      </div>

      <main>
        {sections_html}
      </main>

      <footer>
        <p>Generated from <code>docs/PRODUCT_GUIDE.md</code>. If this page looks stale, the source file has moved on without it — read the source directly or regenerate this page.</p>
      </footer>
    </div>
  </div>
</div>

<script>
(function () {{
  var links = Array.prototype.slice.call(document.querySelectorAll('.nav-item'));
  var sections = links.map(function (l) {{ return document.getElementById(l.getAttribute('data-target')); }}).filter(Boolean);
  if (!('IntersectionObserver' in window) || !sections.length) return;
  var current = null;
  function setActive(id) {{
    if (id === current) return;
    current = id;
    links.forEach(function (l) {{ l.classList.toggle('active', l.getAttribute('data-target') === id); }});
  }}
  var observer = new IntersectionObserver(function (entries) {{
    entries.forEach(function (entry) {{ if (entry.isIntersecting) setActive(entry.target.id); }});
  }}, {{ rootMargin: '-15% 0px -70% 0px', threshold: 0 }});
  sections.forEach(function (s) {{ observer.observe(s); }});
}})();
</script>
"""


def main() -> None:
    md = SRC.read_text()
    chunks = split_on_hr(md)
    if len(chunks) != 4:
        raise SystemExit(
            f"Expected 4 chunks split on '---', got {len(chunks)} — PRODUCT_GUIDE.md structure changed, update this script."
        )

    intro_chunk, maint_chunk, _toc_chunk, rest_chunk = chunks

    intro_lines = intro_chunk.split("\n")
    title_line = next((l for l in intro_lines if l.startswith("# ")), "# QueryGate Product Guide")
    doc_title = title_line[2:].strip()
    intro_html = render_blocks("\n".join(l for l in intro_lines if not l.startswith("# ")))

    maint_lines = maint_chunk.split("\n")
    maint_html = render_blocks("\n".join(l for l in maint_lines if not l.startswith("## ")))

    sections = split_sections(rest_chunk)
    if not sections:
        raise SystemExit(
            "No top-level '## ' sections found in PRODUCT_GUIDE.md — nothing to render."
        )

    section_html_parts = []
    nav_items = []
    for title, body in sections:
        sid = uniq_id(slug(title))
        section_html_parts.append(
            f'<section id="{sid}" class="doc-section">\n<h2>{inline(title)}</h2>\n{render_blocks(body)}\n</section>'
        )
        nav_items.append((sid, title))

    nav_html = "\n".join(
        f'<li><a class="nav-item" href="#{sid}" data-target="{sid}"><span class="nav-dot"></span><span>{html.escape(title, quote=False)}</span></a></li>'
        for sid, title in nav_items
    )

    page = PAGE_TEMPLATE.format(
        brand_orange=BRAND_ORANGE,
        brand_dark=BRAND_DARK,
        section_count=len(sections),
        nav_html=nav_html,
        doc_title=html.escape(doc_title, quote=False),
        intro_html=intro_html,
        maint_html=maint_html,
        sections_html="\n".join(section_html_parts),
    )

    OUT.write_text(page)
    print(f"Wrote {OUT} ({len(page)} bytes, {len(sections)} sections)")


if __name__ == "__main__":
    main()
