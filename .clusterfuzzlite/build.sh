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

for target in "$SRC"/querygate/fuzz/fuzz_*.py; do
  name="$(basename "$target" .py)"
  compile_python_fuzzer "$target"
  echo "built fuzz target: $name"
done
