#!/bin/sh
# Pre-commit guard for the PUBLIC jhirgit/daily-prices repo.
#
# WHY THIS IS NOT jr-dash's tools/sensitive-data-guard.sh. That script was read
# and deliberately not copied: almost all of it is PATH rules for files that do
# not exist in this repo (book.json, book-positions.json, brief.json,
# deploy-jr-dash/state/*, anchor-*.json, statement PDFs, transaction exports),
# and its one prose rule shells out to tools/check-position-data.js, a jr-dash
# script. Copied verbatim it would look like a guard and check almost nothing --
# the #85 failure mode, a check that cannot see its input and says so by not
# saying anything.
#
# This repo's exposure is different and narrower. It is PUBLIC and it holds
# prices, coverage lists and generated payloads. The rule-#18 hazard here is a
# MEMBERSHIP or position fact arriving as PROSE in a comment, a header or a
# README -- which is exactly how `# HELD` / `# REST` / `# FLAT` sat in
# regime.py's coverage pool, in public history, from 2026-08-24 to 2026-09-13.
#
# Install (hooks are shared across all worktrees of this repo):
#   cp tools/sensitive-data-guard.sh "$(git rev-parse --git-common-dir)/hooks/pre-commit"
#   chmod +x "$(git rev-parse --git-common-dir)/hooks/pre-commit"
#
# Override, only when you are certain a match is a false positive:
#   ALLOW_SENSITIVE=1 git commit ...

set -u

# Added lines only. Diff-scoped so a NEW leak is caught without tripping on the
# pre-existing prose that legitimately names these words to DISCLAIM them
# (options_flow.py's "No holding, weight, basis, custodian or account data
# touches this file", README.md on why the options source is not Schwab).
added=$(git diff --cached -U0 --diff-filter=ACM -- \
          . ':(exclude)tools/sensitive-data-guard.sh' \
        | grep '^+' | grep -v '^+++')
[ -z "$added" ] && exit 0

hits=""

# Rule #18 in prose: one of the position words on the same added line as a
# FIGURE. The figure is what separates "no custodian data touches this file"
# (fine, and true) from "held 1,200 shares at a $38.10 basis" (never).
prose=$(printf '%s\n' "$added" \
        | grep -iE '(held|holdings?|cost basis|basis|custodian|Schwab|Chase|Empower|sleeve weight|book value|realized)' \
        | grep -E '\$[0-9]|[0-9][0-9,.]*[[:space:]]*(shares|%)|[0-9][0-9,.]*[kKmM][[:space:]]|[0-9][0-9,.]*[kKmM]$')
[ -n "$prose" ] && hits="$hits
  position facts in prose (rule #18):
$(printf '%s\n' "$prose" | sed 's/^/    /')"

# Membership labels: the exact shape of the leak this hook was written for.
labels=$(printf '%s\n' "$added" \
         | grep -E '^\+[[:space:]]*[#/]+[[:space:]]*(HELD|REST|FLAT)[[:space:]]*$|Book tab|Book-tab|\bnot held\b')
[ -n "$labels" ] && hits="$hits
  book-membership label (which names are held is itself a position fact):
$(printf '%s\n' "$labels" | sed 's/^/    /')"

# Account numbers, SSNs and tokens, wherever they appear.
creds=$(printf '%s\n' "$added" \
        | grep -E '\b[0-9]{3}-[0-9]{5}\b|\b[0-9]{3}-[0-9]{2}-[0-9]{4}\b|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}')
[ -n "$creds" ] && hits="$hits
  account number / SSN / token:
$(printf '%s\n' "$creds" | sed 's/^/    /')"

[ -z "$hits" ] && exit 0

echo ""
echo "  x BLOCKED: this commit adds material that must never reach a PUBLIC repo."
printf '%s\n' "$hits"
echo ""
echo "  Rule #18: share counts, cost basis, custodians, book value, sleeve weights,"
echo "  entry dates -- and WHICH names are held -- live in Google Drive or a"
echo "  gitignored local file. Never in git, and least of all in this repo."
echo "  Unstage with:  git restore --staged <file>"
echo "  Genuine false positive:  ALLOW_SENSITIVE=1 git commit ..."
echo ""
[ "${ALLOW_SENSITIVE:-0}" = "1" ] && {
    echo "  ALLOW_SENSITIVE=1 set -- proceeding anyway."; echo ""; exit 0; }
exit 1
