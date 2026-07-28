"""Pins TODO.md item 124's fix: the `pytest_collection_modifyitems` hook in
`tests/conftest.py` derives a test's tier marker (unit/integration/security)
from its directory, so a file can never again be silently invisible to
`pytest -m unit` (and the pre-commit gate that runs it) the way 52 of 78
`tests/unit/` files were before this hook existed.

Two kinds of coverage, because the fix has two independent properties:

1. Marker-assignment logic (which tier a given path gets, and that an
   already-marked item is left alone) — exercised by calling the real hook
   function directly against fake, filesystem-free `pytest.Item` stand-ins
   (path math only — `Path.relative_to` needs no file to exist). Fast, no
   subprocess.
2. Hookimpl-ordering (`tryfirst`) — a property of how pytest calls *multiple*
   registered `pytest_collection_modifyitems` implementations against each
   other, which a direct function call can't exercise at all (there's only
   one hook in play). This uses the `pytester` fixture to run a real nested
   pytest process, mirroring the shape of `tests/conftest.py`'s real hook
   (a `pytest_collection_modifyitems` that adds a marker) against pytest's
   own built-in `-m` mark-deselection hook, and proves both directions:
   `tryfirst=True` wins the ordering race, `trylast=True` loses it — the
   same pair this repo's own session verified manually when the fix landed
   (see TODO_ARCHIVE.md item 124), now automated instead of one-off.
"""

from __future__ import annotations

import pathlib

import pytest

from tests.conftest import _TESTS_ROOT, pytest_collection_modifyitems

# Deliberately no `pytestmark` here: this file is itself the worked example
# of what item 124's fix covers — a `tests/unit/` file with no explicit tier
# marker, correctly selected by `pytest -m unit` through directory alone.


class _FakeItem:
    """Just enough of the `pytest.Item` surface the hook actually touches."""

    def __init__(self, path: pathlib.Path, markers: list = None):
        self.path = path
        self._markers = list(markers or [])

    def iter_markers(self):
        return iter(self._markers)

    def add_marker(self, marker):
        self._markers.append(marker)


def _marker_names(item: _FakeItem) -> set[str]:
    return {m.name for m in item.iter_markers()}


@pytest.mark.parametrize("tier", ["unit", "integration", "security"])
def test_tags_an_unmarked_test_with_its_directory_tier(tier):
    item = _FakeItem(_TESTS_ROOT / tier / "test_fake.py")

    pytest_collection_modifyitems(items=[item])

    assert _marker_names(item) == {tier}


def test_does_not_double_mark_an_already_marked_test():
    item = _FakeItem(_TESTS_ROOT / "unit" / "test_fake.py", markers=[pytest.mark.unit.mark])

    pytest_collection_modifyitems(items=[item])

    assert list(item.iter_markers()) == [pytest.mark.unit.mark]


def test_leaves_a_test_outside_the_tier_directories_untouched():
    item = _FakeItem(_TESTS_ROOT / "conftest.py")

    pytest_collection_modifyitems(items=[item])

    assert _marker_names(item) == set()


def test_leaves_a_test_outside_tests_root_entirely_untouched():
    item = _FakeItem(pathlib.Path("/some/other/place/test_fake.py"))

    pytest_collection_modifyitems(items=[item])

    assert _marker_names(item) == set()


@pytest.mark.parametrize(
    "hookimpl_kwarg, expect_collected",
    [
        pytest.param("tryfirst=True", True, id="tryfirst-wins-the-ordering-race"),
        pytest.param("trylast=True", False, id="trylast-loses-the-ordering-race"),
    ],
)
def test_hookimpl_order_determines_whether_an_added_mark_affects_m_selection(
    pytester, hookimpl_kwarg, expect_collected
):
    pytester.makeconftest(f"""
        import pytest

        @pytest.hookimpl({hookimpl_kwarg})
        def pytest_collection_modifyitems(items):
            for item in items:
                item.add_marker(pytest.mark.probe)
        """)
    pytester.makepyfile("""
        def test_unmarked():
            pass
        """)

    result = pytester.runpytest_subprocess("-m", "probe", "--collect-only", "-q")

    if expect_collected:
        result.stdout.fnmatch_lines(["*1 test collected*"])
    else:
        result.stdout.fnmatch_lines(["*no tests collected*"])
