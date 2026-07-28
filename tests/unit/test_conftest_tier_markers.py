"""Pins TODO.md item 124's fix: the `pytest_collection_modifyitems` hook in
`tests/conftest.py` derives a test's tier marker (unit/integration/security)
from its directory, so a file can never again be silently invisible to
`pytest -m unit` (and the pre-commit gate that runs it) the way 52 of 78
`tests/unit/` files were before this hook existed.

Exercises the real hook function directly against fake, filesystem-free
`pytest.Item` stand-ins (path math only — `Path.relative_to` needs no file to
exist) rather than a full nested pytest run, so this stays a fast, dependency-
free unit test. It does not re-prove the `tryfirst` hookimpl-ordering
property — that pytest-hook-call-order guarantee was mutation-verified
interactively when the fix landed (`trylast` reproducibly broke `-m unit`
back down to the pre-fix 726-test selection; see TODO_ARCHIVE.md item 124)
and isn't the kind of thing a direct function call can exercise, since
ordering is a property of how pytest invokes multiple registered hookimpls,
not of the function body itself.
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
