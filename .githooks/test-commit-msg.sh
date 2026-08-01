#!/bin/sh
# Test suite for .githooks/commit-msg.
#
# The hook is an enforcement point, and this repo's own rule is that a rule
# with no test that fails when it breaks is a comment, not a control. So every
# alternative in the hook's pattern gets a fixture that only that alternative
# can catch (not shadowed by a sibling alternative also matching), the hook's
# "never blocks the commit" contract is asserted on every case (not just its
# text output), and a multi-trailer message proves the strip removes every AI
# trailer while leaving human ones alone.
#
# Runs against scratch message files under $TMPDIR — it never touches the real
# tree.
#
#   sh .githooks/test-commit-msg.sh
#
set -u

HOOK=$(cd "$(dirname "$0")" && pwd)/commit-msg
[ -f "$HOOK" ] || { echo "cannot find commit-msg next to $0" >&2; exit 1; }

pass=0; fail=0
RED=''; GRN=''; OFF=''
if [ -t 1 ]; then RED='\033[31m'; GRN='\033[32m'; OFF='\033[0m'; fi

WORK=$(mktemp -d "${TMPDIR:-/tmp}/commit-msg-test.XXXXXX") || exit 1
trap 'rm -rf "$WORK"' EXIT INT TERM

# run_hook <message-text> -> writes resulting message to $WORK/msg, rc to $WORK/rc
run_hook() {
  printf '%s\n' "$1" > "$WORK/msg"
  sh "$HOOK" "$WORK/msg" >"$WORK/out" 2>&1
  echo $? > "$WORK/rc"
}

ok() { printf "${GRN}PASS${OFF}  %s\n" "$1"; pass=$((pass + 1)); }
bad() { printf "${RED}FAIL${OFF}  %s (%s)\n" "$1" "$2"; sed 's/^/        | /' "$WORK/out" "$WORK/msg" 2>/dev/null; fail=$((fail + 1)); }

# assert_stripped <name> <message> — every trailer gone, rc==0, subject line survives.
assert_stripped() {
  name=$1; msg=$2
  run_hook "$msg"
  rc=$(cat "$WORK/rc")
  if [ "$rc" -ne 0 ]; then bad "$name" "hook exited $rc, contract is never-block"; return; fi
  if grep -qi 'Co-Authored-By' "$WORK/msg"; then bad "$name" "trailer survived"; return; fi
  if ! grep -q '^fix: thing$' "$WORK/msg"; then bad "$name" "subject line was destroyed, not just the trailer"; return; fi
  ok "$name"
}

# assert_kept <name> <message> — trailer(s) still present, rc==0.
assert_kept() {
  name=$1; msg=$2
  run_hook "$msg"
  rc=$(cat "$WORK/rc")
  if [ "$rc" -ne 0 ]; then bad "$name" "hook exited $rc, contract is never-block"; return; fi
  if ! grep -qi 'Co-Authored-By' "$WORK/msg"; then bad "$name" "trailer was stripped but should not have been"; return; fi
  ok "$name"
}

# assert_unchanged <name> <message> — byte-for-byte identical output, rc==0.
assert_unchanged() {
  name=$1; msg=$2
  printf '%s\n' "$msg" > "$WORK/before"
  run_hook "$msg"
  rc=$(cat "$WORK/rc")
  if [ "$rc" -ne 0 ]; then bad "$name" "hook exited $rc, contract is never-block"; return; fi
  if ! diff -q "$WORK/before" "$WORK/msg" >/dev/null 2>&1; then bad "$name" "message was modified but should not have been"; return; fi
  ok "$name"
}

# --- one fixture per pattern alternative, none shadowed by a sibling ------
# (a fixture using an @anthropic.com email would let `anthropic` alone catch
# it regardless of the alternative under test — every fixture below avoids
# that so deleting any single alternative breaks exactly one case.)

assert_stripped "strips claude — bare word, non-anthropic address" \
  "$(printf 'fix: thing\n\nCo-Authored-By: Claude <claude@example.org>')"

assert_stripped "strips anthropic — brand name, non-anthropic.com address" \
  "$(printf 'fix: thing\n\nCo-Authored-By: Anthropic Assistant <bot@anthropic-ai.example>')"

assert_stripped "strips openai" \
  "$(printf 'fix: thing\n\nCo-Authored-By: OpenAI Codex <bot@openai.example>')"

assert_stripped "strips chatgpt" \
  "$(printf 'fix: thing\n\nCo-Authored-By: ChatGPT <bot@example.com>')"

assert_stripped "strips gpt-" \
  "$(printf 'fix: thing\n\nCo-Authored-By: GPT-4 Agent <bot@example.com>')"

assert_stripped "strips copilot" \
  "$(printf 'fix: thing\n\nCo-Authored-By: GitHub Copilot <copilot@github.example>')"

assert_stripped "strips gemini" \
  "$(printf 'fix: thing\n\nCo-Authored-By: Google Gemini <bot@example.com>')"

# --- realistic Claude Code trailer, case-insensitive header key -----------

assert_stripped "strips real Claude Sonnet trailer, lowercase header key" \
  "$(printf 'fix: thing\n\nco-authored-by: Claude Sonnet 5 <noreply@anthropic.com>')"

# --- negative controls: humans are never caught ----------------------------

assert_kept "leaves ordinary human co-author alone" \
  "$(printf 'fix: thing\n\nCo-Authored-By: Jane Doe <jane@example.com>')"

assert_kept "leaves a human whose name CONTAINS \"claude\" as a substring alone (word-boundary, not string-inequality)" \
  "$(printf 'fix: thing\n\nCo-Authored-By: Ann Mclaude <ann@example.com>')"

assert_unchanged "leaves message with no trailer alone" \
  "$(printf 'fix: thing\n\nplain body, no trailer here')"

# --- multiple trailers: strips every AI one, keeps every human one --------

multi=$(printf 'fix: thing\n\nCo-Authored-By: Jane Doe <jane@example.com>\nCo-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>\nCo-Authored-By: GitHub Copilot <copilot@github.example>')
run_hook "$multi"
rc=$(cat "$WORK/rc")
name="multi-trailer message: strips both AI trailers, keeps the human one"
if [ "$rc" -ne 0 ]; then
  bad "$name" "hook exited $rc, contract is never-block"
elif grep -qi 'claude\|copilot' "$WORK/msg"; then
  bad "$name" "an AI trailer survived"
elif ! grep -q 'Jane Doe' "$WORK/msg"; then
  bad "$name" "the human trailer was wrongly removed"
else
  ok "$name"
fi

# --- hook never blocks even on a malformed invocation ----------------------

sh "$HOOK" >"$WORK/out" 2>&1
rc=$?
if [ "$rc" -eq 0 ]; then
  ok "hook exits 0 when invoked with no message-file argument"
else
  bad "hook exits 0 when invoked with no message-file argument" "exited $rc instead"
fi

echo
if [ "$fail" -eq 0 ]; then
  printf "${GRN}%d passed, 0 failed${OFF}\n" "$pass"
else
  printf "${RED}%d passed, %d failed${OFF}\n" "$pass" "$fail"
fi
[ "$fail" -eq 0 ]
