"""`docs/product-guide.html` must not drift from its markdown source (item 193).

The HTML is a generated, git-committed rendering of `docs/PRODUCT_GUIDE.md`, and
it is the copy most likely to be *shared* — it is the browsable one. Nothing
enforced that it matched its source, so it went stale silently: it was found
three whole sections plus two Decision Log entries behind, and stale since
`f995e35` rather than merge-caused.

`tests/unit/test_product_guide.py` does **not** cover this — it exercises the
packaged in-product help corpus, which is a different artifact. A reviewer told
me otherwise while auditing this branch; that was wrong, and this file is what
makes the question answerable instead of arguable.

Same posture as `test_trust_page.py` for `docs/TRUST_EVIDENCE.md` and
`test_third_party_licenses.py` for the dependency inventory: regenerate in
memory, compare, and name the command that fixes it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import generate_product_guide_html

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_HTML = _ROOT / "docs" / "product-guide.html"
_MARKDOWN = _ROOT / "docs" / "PRODUCT_GUIDE.md"


def _regenerate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run the real generator into a throwaway path and return what it wrote."""
    out = tmp_path / "product-guide.html"
    monkeypatch.setattr(generate_product_guide_html, "OUT", out)
    generate_product_guide_html.main()
    return out.read_text(encoding="utf-8")


def test_committed_html_matches_the_markdown_source(tmp_path, monkeypatch):
    assert _HTML.is_file(), "run `make product-guide-html`"
    regenerated = _regenerate(tmp_path, monkeypatch)
    assert _HTML.read_text(encoding="utf-8") == regenerated, (
        "docs/product-guide.html has drifted from docs/PRODUCT_GUIDE.md — regenerate it "
        "with `make product-guide-html`. It is the copy people share, so a stale one is "
        "worse than none."
    )


def test_the_guard_would_catch_a_source_edit(tmp_path, monkeypatch):
    """Negative control: an edit to the markdown must change the output.

    Without this, a generator that ignored its input would satisfy the test
    above forever.
    """
    original = _MARKDOWN.read_text(encoding="utf-8")
    baseline = _regenerate(tmp_path, monkeypatch)
    marker = "QueryGate drift sentinel 8f2a1c"
    assert marker not in baseline
    try:
        _MARKDOWN.write_text(
            original.replace("## Decision Log", f"## Decision Log\n\n{marker}\n", 1),
            encoding="utf-8",
        )
        assert marker in _regenerate(tmp_path, monkeypatch)
    finally:
        _MARKDOWN.write_text(original, encoding="utf-8")
    assert _MARKDOWN.read_text(encoding="utf-8") == original


def test_every_markdown_section_reaches_the_rendered_page(tmp_path, monkeypatch):
    """Byte-equality alone would be satisfied by a generator that silently
    dropped sections, as long as the committed copy dropped them too. The
    original defect was measured in *missing sections*, so count them."""
    rendered = _regenerate(tmp_path, monkeypatch)
    chunks = generate_product_guide_html.split_on_hr(_MARKDOWN.read_text(encoding="utf-8"))
    sections = generate_product_guide_html.split_sections(chunks[3])
    assert len(sections) >= 10, "PRODUCT_GUIDE.md structure changed unexpectedly"
    assert rendered.count('class="doc-section"') == len(sections)
