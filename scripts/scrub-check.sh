#!/usr/bin/env bash
# scrub-check.sh — gate that fails if credential-shaped strings or
# private-infrastructure references appear anywhere they could ship:
#
#   corpus 1: the tracked working tree (git grep)
#   corpus 2: full git history diffs (git log -p --all)
#   corpus 3: commit metadata — author/committer names+emails, commit
#             messages — plus configured remote URLs
#
# Two pattern layers:
#   1. Built-in patterns (below): things that are never OK in a public repo.
#   2. Optional extra patterns: one extended-regex per line in a gitignored
#      `.scrub-extra-patterns` file at the repo root (or a file pointed to by
#      SCRUB_EXTRA_PATTERNS). Use this for identifiers private to YOUR
#      deployment — hostnames, usernames, room IDs — without publishing the
#      list itself. Extra patterns are matched case-insensitively.
#
# Runs from CI and from .githooks/pre-push. This script excludes itself from
# every corpus (it necessarily contains the patterns); built-ins also use
# [c]har-class tricks so they never literally self-match.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

SELF="scripts/scrub-check.sh"
fail=0

hits="$(mktemp)"
hist="$(mktemp)"
meta="$(mktemp)"
trap 'rm -f "$hits" "$hist" "$meta"' EXIT

# Build the history + metadata corpora once. `--all` covers every ref;
# CI must check out with fetch-depth: 0 or history scanning is vacuous.
git log -p --all --full-history -- . ":(exclude)$SELF" > "$hist" 2>/dev/null || true
{
    git log --all --format='commit %H%nauthor: %an <%ae>%ncommitter: %cn <%ce>%n%B'
    git config --get-regexp '^remote\..*\.url' 2>/dev/null || true
} > "$meta"

scan() {
    local label="$1" pattern="$2" flags="${3:-}"
    if git grep -nEI $flags -- "$pattern" -- \
        ":(exclude).scrub-extra-patterns" ":(exclude)$SELF" > "$hits" 2>/dev/null; then
        echo "SCRUB FAIL [$label] (working tree):"
        cat "$hits"
        fail=1
    fi
    if grep -nE $flags -- "$pattern" "$hist" > "$hits" 2>/dev/null; then
        echo "SCRUB FAIL [$label] (git history — git log -p):"
        head -10 "$hits"
        fail=1
    fi
    if grep -nE $flags -- "$pattern" "$meta" > "$hits" 2>/dev/null; then
        echo "SCRUB FAIL [$label] (commit metadata / remotes):"
        cat "$hits"
        fail=1
    fi
}

# --- built-in: credential shapes ---
# NOTE: no \b anchors here — macOS `git grep -E` is POSIX ERE without the
# GNU \b extension, and an unsupported anchor silently matches nothing,
# disabling the pattern on that platform. Use portable (^|[^…]) instead.
scan "matrix-access-token"   '(^|[^A-Za-z0-9_])s[y]t_[A-Za-z0-9_-]{8,}'
scan "private-key-block"     'BEGIN (RSA|EC|OPENSSH|DSA|PGP) [P]RIVATE KEY'
scan "bearer-literal"        "Authorization['\"]?[[:space:]]*[:=][[:space:]]*['\"]Bearer [A-Za-z0-9+/_-]{16,}"

# --- built-in: private-network shapes ---
scan "tailnet-hostname"      'ta[i]l[0-9a-f]{4,}\.ts\.net'
scan "ts-net-hostname"       '[A-Za-z0-9-]+\.t[s]\.net'
scan "cgnat-ip"              '(^|[^0-9.])100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}($|[^0-9])'

# --- built-in: env hygiene ---
if git ls-files | grep -xE '\.env|\.env\.[^e].*|.*/\.env' >/dev/null 2>&1; then
    echo "SCRUB FAIL [tracked-dotenv]: a .env file is tracked by git"
    git ls-files | grep -xE '\.env|\.env\.[^e].*|.*/\.env'
    fail=1
fi

# --- built-in: commit identity allowlist ---
# Guards against a stray local git identity (env vars override -c and config)
# stamping personal details into public history.
ALLOWED_IDENTITY='Rommy <255708385+cosmic-fire-eng@users.noreply.github.com>'
if git log --all --format='%an <%ae>%n%cn <%ce>' | sort -u \
        | grep -vxF "$ALLOWED_IDENTITY" > "$hits"; then
    echo "SCRUB FAIL [commit-identity]: commits by an unexpected author/committer:"
    cat "$hits"
    fail=1
fi

# --- optional extra patterns (kept out of the repo; case-insensitive) ---
extra="${SCRUB_EXTRA_PATTERNS:-.scrub-extra-patterns}"
if [[ -f "$extra" ]]; then
    while IFS= read -r line; do
        [[ -z "$line" || "$line" == \#* ]] && continue
        scan "extra-pattern: ${line:0:3}…" "$line" "-i"
    done < "$extra"
fi

if [[ "$fail" -ne 0 ]]; then
    echo
    echo "Scrub gate failed — remove the strings above before pushing."
    echo "(History hits require rewriting/redoing the commit, not just editing files.)"
    exit 1
fi
echo "Scrub gate passed (tree + history + commit metadata)."
