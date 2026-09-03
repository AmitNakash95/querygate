#!/bin/bash -eu
#
# ClusterFuzzLite build script — compiles every target in fuzz/ into $OUT.
#
# Deliberately installs the project without dev dependencies: the fuzz targets
# import `querygate.query_ast`, which needs the runtime dependency set only.
# Pulling the dev group in would drag pytest, hypothesis and the whole tooling
# tree into the fuzzing image for no coverage benefit and a slower build.

# Pinned for the same reason as the root Dockerfile: an unpinned upgrade
# makes the fuzzing image non-reproducible between runs.
python3 -m pip install "pip==26.2.1"
# The project itself. `.` rather than the wheel so a fuzzing run always tracks
# the working tree, which is what a PR-triggered run needs.
python3 -m pip install .

# --collect-data/--hidden-import examples is load-bearing, not belt-and-braces.
# `querygate.policy.models` pulls `core/config.py`, which resolves the `examples`
# package through `importlib.resources` at MODULE scope. That is invisible to
# PyInstaller's static import analysis, so the packaged binary died at startup
# with `ModuleNotFoundError: No module named 'examples'` — and only for the
# target that imports Policy, which is why the first fuzz target never hit it.
for target in "$SRC"/querygate/fuzz/fuzz_*.py; do
  name="$(basename "$target" .py)"
  compile_python_fuzzer "$target" \
    --collect-data examples \
    --hidden-import examples
  echo "built fuzz target: $name"
done

# Seed corpus for the policy target. Measured why this is required, not nice to
# have: without it the target plateaued at 29 coverage features over 10,993,940
# executions, because random bytes essentially never satisfy the StructuredQuery
# model and `validate_structural_caps` was therefore never reached. The corpus
# stalled at five inputs totalling seven bytes. Seeds start the fuzzer INSIDE
# the boundary so it mutates outward from valid ASTs.
python3 "$SRC"/querygate/fuzz/make_seed_corpus.py /tmp/qg-seeds
(cd /tmp/qg-seeds && zip -q -r "$OUT/fuzz_policy_validation_seed_corpus.zip" .)
echo "seed corpus: $(ls /tmp/qg-seeds | wc -l) documents"
