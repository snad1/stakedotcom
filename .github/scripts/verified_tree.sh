#!/usr/bin/env bash
# CI only. Tells a verify job whether the exact code it checked out has already passed
# verification in an earlier run, so a merge that moves the same code to the next branch
# does not repeat the same checks. The key is the git tree SHA: it names the file contents,
# so it survives merge commits but changes with any edit, including lockfiles, tool version
# files and the workflows themselves. A successful full run uploads an artifact named
# verified-<salt>-<tree>. Artifacts are repo-wide; Actions caches are branch-scoped, so a
# cache cannot carry the marker from one branch to the next.
#
# Stack-agnostic: it knows nothing about the language or the checks being skipped.
#
# Env: GH_TOKEN (needs the actions: read permission), GITHUB_REPOSITORY, GITHUB_OUTPUT
#      VERIFY_SALT (optional) names the check set this marker stands for; default v1
# Writes to GITHUB_OUTPUT: tree, marker, verified (true | false)
#
# Usage, from the repo root:
#   bash tool/ci/verified_tree.sh
set -euo pipefail

# Namespaces the marker. Two workflows that run DIFFERENT checks must not share one: the
# marker means "this tree passed these checks", so a shared name lets one skip on the
# other's evidence, e.g. a rule scanner passing would excuse the whole test suite.
# Workflows that run the SAME checks should share it deliberately. Bump the version part
# to make every tree verify again, e.g. after a runner or toolchain change.
salt="${VERIFY_SALT:-v1}"

: "${GITHUB_REPOSITORY:?}" "${GITHUB_OUTPUT:?}"

tree="$(git rev-parse 'HEAD^{tree}')"
marker="verified-$salt-$tree"

# Any lookup failure falls back to a full run: skipping must never hide unverified code.
count="$(gh api "repos/$GITHUB_REPOSITORY/actions/artifacts?name=$marker&per_page=5" \
    --jq '[.artifacts[] | select(.expired == false)] | length' 2>/dev/null)" || count=0
[[ "$count" =~ ^[0-9]+$ ]] || count=0

verified=false
if ((count > 0)); then verified=true; fi

{
    echo "tree=$tree"
    echo "marker=$marker"
    echo "verified=$verified"
} >> "$GITHUB_OUTPUT"

if [[ "$verified" == true ]]; then
    echo "tree $tree already passed verification; skipping it"
else
    echo "tree $tree has no passing run yet; running full verification"
fi
