#!/bin/sh
# Test suite for the three new mechanical checks added to .githooks/pre-commit
# (merge-conflict markers, secret-shaped content, oversized blobs). The
# pre-existing black/pytest checks are exercised by the ordinary dev loop and
# CI, not re-verified here — this suite covers the enforcement points this
# hardening pass actually added, per this repo's own mutation-verify rule: a
# check with no test that fails when it breaks is a comment, not a control.
#
# Runs in a scratch git repo under $TMPDIR — it never touches the real tree.
# Since the scratch repo has no pyproject.toml/poetry env, a hook run that
# gets past all three new checks is expected to fail later at the black step
# ("poetry: command not found" or similar) rather than succeed outright —
# tests for that case assert the *new* checks stayed silent, not that the
# whole hook exited 0.
#
# Secret-pattern fixtures are built with printf's format/argument split (as
# the reference template does) so the fixture text this file writes into a
# scratch repo is never itself a contiguous credential-shaped string sitting
# in THIS script's own source — otherwise committing this file would trip the
# very check it tests.
#
#   sh .githooks/test-pre-commit.sh
#
set -u

HOOK=$(cd "$(dirname "$0")" && pwd)/pre-commit
[ -f "$HOOK" ] || { echo "cannot find pre-commit next to $0" >&2; exit 1; }

pass=0; fail=0
RED=''; GRN=''; OFF=''
if [ -t 1 ]; then RED='\033[31m'; GRN='\033[32m'; OFF='\033[0m'; fi

WORK=$(mktemp -d "${TMPDIR:-/tmp}/pre-commit-test.XXXXXX") || exit 1
trap 'rm -rf "$WORK"' EXIT INT TERM

# fresh scratch repo with the hook installed
setup() {
  rm -rf "$WORK/repo"; mkdir -p "$WORK/repo/.githooks"
  git -C "$WORK/repo" init -q
  git -C "$WORK/repo" config user.email t@t.t
  git -C "$WORK/repo" config user.name t
  cp "$HOOK" "$WORK/repo/.githooks/pre-commit"
  chmod +x "$WORK/repo/.githooks/pre-commit"
}

stage() { git -C "$WORK/repo" add -A >/dev/null 2>&1; }

# run_hook -> writes combined output to $WORK/out, returns exit code
run_hook() {
  ( cd "$WORK/repo" && bash .githooks/pre-commit ) >"$WORK/out" 2>&1
}

ok() { printf "${GRN}PASS${OFF}  %s\n" "$1"; pass=$((pass + 1)); }
bad() { printf "${RED}FAIL${OFF}  %s (%s)\n" "$1" "$2"; sed 's/^/        | /' "$WORK/out"; fail=$((fail + 1)); }

# assert_blocked <name> <rc> <expected-substring> — pass $? captured
# immediately after run_hook; capturing it inside this function would instead
# see the exit status of this function's own argument assignments.
assert_blocked() {
  name=$1; rc=$2; want=$3
  if [ "$rc" -eq 0 ]; then bad "$name" "hook exited 0, expected a BLOCK"; return; fi
  if ! grep -q "$want" "$WORK/out"; then bad "$name" "did not see expected message: $want"; return; fi
  ok "$name"
}

# --- 1. Merge-conflict markers ---------------------------------------------

setup
{
  printf 'def f():\n'
  printf '%s HEAD\n' '<<<<<<<'
  printf '    return 1\n'
  printf '%s\n' '======='
  printf '%s branch\n' '>>>>>>>'
} > "$WORK/repo/conflicted.py"
stage
run_hook; rc=$?
assert_blocked "blocks unresolved merge-conflict markers" "$rc" "unresolved merge-conflict markers"

# --- 2. Secret-shaped content — one fixture per alternative, each built with
#        a format/argument split so the pattern never appears whole in this
#        script's own source text. -----------------------------------------

setup; printf -- '-----BEGIN %s PRIVATE KEY-----\nMIIBogIBAAJ...\n' RSA > "$WORK/repo/key.pem"; stage
run_hook; rc=$?
assert_blocked "blocks a PEM private key header" "$rc" "credential-shaped string"

setup; printf 'AWS_KEY = "AKIA%s"\n' "ABCDEFGHIJKLMNOP" > "$WORK/repo/config.py"; stage
run_hook; rc=$?
assert_blocked "blocks an AWS access key ID" "$rc" "credential-shaped string"

setup; printf 'token: ghp_%s\n' "abcdefghij0123456789ABCDEFGHIJ012345" > "$WORK/repo/notes.txt"; stage
run_hook; rc=$?
assert_blocked "blocks a GitHub personal access token" "$rc" "credential-shaped string"

setup; printf 'SLACK_TOKEN=xox%s-%s\n' "b" "abcdefghij" > "$WORK/repo/notes.txt"; stage
run_hook; rc=$?
assert_blocked "blocks a Slack token" "$rc" "credential-shaped string"

setup; printf 'OPENAI_API_KEY=sk-%s\n' "abcdefghijklmnopqrstuvwxyz0123456789ABCD" > "$WORK/repo/notes.txt"; stage
run_hook; rc=$?
assert_blocked "blocks an OpenAI-style sk- key" "$rc" "credential-shaped string"

# --- 3. Oversized blobs ------------------------------------------------------

setup
# 6MB of zero bytes — over the 5MB threshold.
dd if=/dev/zero of="$WORK/repo/big.bin" bs=1048576 count=6 >/dev/null 2>&1
stage
run_hook; rc=$?
assert_blocked "blocks a staged blob over 5MB" "$rc" "MB staged"

setup
# The oversized-blob check must self-exclude too, same as checks 1 and 2 —
# pad a copy of the hook itself past the threshold and confirm it is not
# blocked on its own size.
cp "$HOOK" "$WORK/repo/.githooks/pre-commit"
dd if=/dev/zero bs=1048576 count=6 >> "$WORK/repo/.githooks/pre-commit" 2>/dev/null
stage
run_hook
name="self-exclusion: the hook's own file padded past 5MB is not blocked on its own size"
if grep -q "MB staged" "$WORK/out"; then
  bad "$name" "the hook flagged its own (self-excluded) file as an oversized blob"
else
  ok "$name"
fi

# --- Negative controls: none of the three new checks false-positive --------

setup
cat > "$WORK/repo/clean.py" <<'PY'
def add(a, b):
    return a + b
PY
stage
run_hook
name="clean file trips none of the three new checks"
if grep -qE "unresolved merge-conflict markers|credential-shaped string|MB staged" "$WORK/out"; then
  bad "$name" "a new check false-positived on an unrelated clean file"
else
  ok "$name"
fi

setup
# A small (under-threshold) binary-ish file must not trip the blob-size check.
dd if=/dev/zero of="$WORK/repo/small.bin" bs=1024 count=10 >/dev/null 2>&1
stage
run_hook
name="a small staged blob (10KB) does not trip the oversized-blob check"
if grep -q "MB staged" "$WORK/out"; then
  bad "$name" "false-positived under the 5MB threshold"
else
  ok "$name"
fi

# The hook's own source defines these regexes as literal text, which happens
# not to match any of them (verified separately) — so committing an
# unrelated change alongside the hook staying unblocked, by itself, proves
# nothing about the `[ "$f" = "$self" ] && continue` self-exclusion line;
# the test would pass identically if that line were deleted. Exercise the
# exclusion for real: append a genuinely-matching secret to the copied hook
# file and confirm it is NOT blocked, with a sibling control proving the
# identical string IS blocked when it isn't self.
setup
cp "$HOOK" "$WORK/repo/.githooks/pre-commit"
printf 'AKIA%s\n' "ABCDEFGHIJKLMNOP" >> "$WORK/repo/.githooks/pre-commit"
stage
run_hook
name="self-exclusion: a genuinely-matching secret appended to the hook's own file is not blocked"
if grep -q "credential-shaped string" "$WORK/out"; then
  bad "$name" "the hook flagged its own (self-excluded) file despite the exclusion"
else
  ok "$name"
fi

setup
printf 'AKIA%s\n' "ABCDEFGHIJKLMNOP" > "$WORK/repo/unrelated.txt"
stage
run_hook; rc=$?
assert_blocked "self-exclusion control: the identical string in a non-hook file IS blocked" "$rc" "credential-shaped string"

echo
if [ "$fail" -eq 0 ]; then
  printf "${GRN}%d passed, 0 failed${OFF}\n" "$pass"
else
  printf "${RED}%d passed, %d failed${OFF}\n" "$pass" "$fail"
fi
[ "$fail" -eq 0 ]
