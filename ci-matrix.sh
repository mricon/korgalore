#!/usr/bin/env sh

set -eu

# Run an import smoke check and pytest under every interpreter version in the
# supported range (see `requires-python` in pyproject.toml). This is not part
# of ci.sh because materialising a venv per version is slow; invoke this
# before releases and after changes that could depend on version-specific
# runtime behaviour (import-time annotations, stdlib APIs, etc.).
#
# The linters and type checkers are deliberately not repeated here. ci.sh runs
# them once, and they are configured against a fixed target version
# (target-version/python_version in pyproject.toml), so re-running them per
# interpreter would only report the same thing again for each one.
#
# Missing interpreters are pulled automatically from uv's managed Python
# cache (python-build-standalone), so no system packages or sudo are needed.
#
# Override the version list: PYTHONS="3.11 3.14" ./ci-matrix.sh
#
# After the interpreter sweep it runs one "floor" lane: the project resolved
# to the *minimum* versions our metadata allows (uv --resolution
# lowest-direct), then the suite. This is what catches a dependency floor that
# is declared but does not actually work -- our `liblore>=0.9.0` and
# `click>=8.1.7` bounds are claims about what we support, and every other lane
# resolves the newest compatible release instead, so nothing else tests them.
# It runs on the lowest supported interpreter, where the old dependency
# releases are likeliest to still publish wheels. Override or skip it:
# FLOOR_PY=3.12 ./ci-matrix.sh  (or FLOOR_PY= ./ci-matrix.sh to skip)
#
# After the floor lane it runs one advisory "prerelease" lane, on the newest
# CPython not yet in PYTHONS (a beta ahead of its final release, say), so we
# get an early signal without waiting on a relock. It resolves fresh against
# PyPI instead of uv.lock, and never fails the run -- see the lane itself for
# why. It is off until uv can install the next CPython; turn it on with
# PRERELEASE_PY=3.16 ./ci-matrix.sh

# ${VAR-default} rather than ${VAR:-default}: the latter also substitutes for
# an explicitly empty value, which would make "PYTHONS= ./ci-matrix.sh" run
# the full sweep instead of skipping it.
PYTHONS="${PYTHONS-3.11 3.12 3.13 3.14 3.15}"

# Install any requested interpreters that are missing. This is an explicit
# step because a dev may have set UV_PYTHON_DOWNLOADS=manual globally to
# prevent `uv sync` from reaching out to the network mid-workflow; running
# it once up front scopes the network access to this script.
if [ -n "$PYTHONS" ]; then
    # shellcheck disable=SC2086
    uv python install $PYTHONS
fi

# Collect failures so the run reports a complete matrix instead of bailing on
# the first broken interpreter.
failed=""

for py in $PYTHONS; do
    printf '\n=== Python %s ===\n' "$py"
    # Each version gets its own project environment so switching interpreters
    # does not thrash the default .venv and each sync is incremental.
    UV_PROJECT_ENVIRONMENT=".venv-$py"
    export UV_PROJECT_ENVIRONMENT
    # No --all-extras: the gui extra needs PyGObject, which builds against
    # cairo and girepository headers. See the same note in ci.sh.
    if ! uv sync --all-groups --python "$py"; then
        failed="$failed $py(sync)"
        continue
    fi
    if ! uv run python -c 'import korgalore.cli, sys; print("import korgalore.cli OK on", sys.version.split()[0])'; then
        failed="$failed $py(import)"
        continue
    fi
    # The CLI is the entry point users actually hit, so make sure Click can
    # build the command tree under this interpreter, not just that the
    # package imports.
    if ! uv run kgl --version; then
        failed="$failed $py(cli)"
        continue
    fi
    if ! uv run pytest --durations=20; then
        failed="$failed $py(pytest)"
        continue
    fi
done

# Floor lane: install the project at the minimum versions our metadata allows,
# then run the suite. Dependency groups (pytest, mypy, ...) carry no lower
# bounds, so `uv sync --resolution lowest-direct` would drag them to
# unbuildable ancient releases; installing just the project with
# `uv pip install` keeps the flooring to korgalore's own runtime dependencies,
# and a current test runner is layered on afterwards. This does not cover the
# `gui` extra's `PyGObject>=3.42.0` floor -- it needs cairo/girepository
# headers, per the `uv sync` comment above -- so that one is untested here;
# misc/distro/ is what actually covers it.
FLOOR_PY="${FLOOR_PY-3.11}"
if [ -n "$FLOOR_PY" ]; then
    printf '\n=== Floors (lowest-direct) on Python %s ===\n' "$FLOOR_PY"
    # --python targets the venv explicitly, so the loop's UV_PROJECT_ENVIRONMENT
    # must not leak in and redirect these commands.
    unset UV_PROJECT_ENVIRONMENT
    uv python install "$FLOOR_PY"
    floorenv='.venv-floor'
    rm -rf "$floorenv"
    if ! uv venv "$floorenv" --python "$FLOOR_PY"; then
        failed="$failed floors(venv)"
    elif ! uv pip install --python "$floorenv" --resolution lowest-direct .; then
        failed="$failed floors(install)"
    elif ! uv pip install --python "$floorenv" pytest; then
        failed="$failed floors(pytest-install)"
    elif ! "$floorenv/bin/python" -c 'import korgalore.cli, sys; print("import korgalore.cli OK on", sys.version.split()[0])'; then
        failed="$failed floors(import)"
    elif ! "$floorenv/bin/kgl" --version; then
        failed="$failed floors(cli)"
    elif ! "$floorenv/bin/python" -m pytest --durations=20; then
        failed="$failed floors(pytest)"
    fi

    # Report what the floors actually resolved to. A green lane says the
    # declared minimums install and pass; this says which versions that was,
    # so a bound that has drifted above what PyPI still offers is visible.
    #
    # The names to report are exactly the runtime deps that declare a lower
    # bound, so derive them from pyproject.toml rather than keeping a second
    # copy here that drifts out of sync (a plain 'google' prefix, e.g., would
    # also absorb transitive google-api-core and googleapis-common-protos,
    # which declare no floor of ours). Anchored at both ends to match whole
    # package names in the `uv pip list` table.
    bounded=$(sed -n '/^dependencies = \[/,/^]/p' pyproject.toml \
        | sed -n 's/^[[:space:]]*"\([A-Za-z0-9._-]*\)[[:space:]]*>=.*/\1/p' \
        | paste -sd'|' -)
    printf '\n--- resolved floors ---\n'
    if [ -n "$bounded" ]; then
        uv pip list --python "$floorenv" 2>/dev/null \
            | grep -Ei "^($bounded)[[:space:]]" || true
    else
        printf 'could not derive bounded deps from pyproject.toml\n'
    fi
fi

# Prerelease lane: try the newest CPython not yet in PYTHONS, such as a beta
# ahead of its final release. This resolves fresh against PyPI instead of
# the committed uv.lock, which only targets our declared requires-python
# range and would otherwise need relocking (and could fail to resolve at
# all) just to humor an interpreter we don't support yet.
#
# Ecosystem wheels (cffi, cryptography, ...) typically lag a new CPython
# release by weeks to months, so failures here are expected and this lane
# is advisory: it never fails the run, only reports what broke, so it does
# not block CI while giving a heads-up for when to move the interpreter
# into PYTHONS for real.
#
# `uv python install` alone leaves an already-installed interpreter in place
# even if a newer patch (e.g. a later beta) has since shipped, which is the
# opposite of what a prerelease lane is for -- pass --upgrade so it always
# fetches the newest patch uv knows about. That "knows about" is doing real
# work: uv's download catalog ships with the uv binary itself, so a stale uv
# install can still resolve a stale prerelease even with --upgrade. Print
# `uv --version` alongside the resolved interpreter so that is visible
# without having to dig through a failure traceback.
# Off by default while there is no newer CPython than the last one in PYTHONS
# for uv to install. Set the default to 3.16 once `uv python list 3.16` shows
# an alpha; until then, run it with PRERELEASE_PY=3.16 ./ci-matrix.sh
PRERELEASE_PY="${PRERELEASE_PY-}"
if [ -n "$PRERELEASE_PY" ]; then
    printf '\n=== Prerelease (advisory) on Python %s ===\n' "$PRERELEASE_PY"
    uv --version
    unset UV_PROJECT_ENVIRONMENT
    prereleaseenv=".venv-prerelease"
    rm -rf "$prereleaseenv"
    prerelease_failed=""
    if ! uv python install "$PRERELEASE_PY" --upgrade; then
        prerelease_failed="install"
    elif ! uv venv "$prereleaseenv" --python "$PRERELEASE_PY"; then
        prerelease_failed="venv"
    elif ! uv pip install --python "$prereleaseenv" .; then
        prerelease_failed="install-project"
    elif ! uv pip install --python "$prereleaseenv" pytest; then
        prerelease_failed="pytest-install"
    elif ! "$prereleaseenv/bin/python" -c 'import korgalore.cli, sys; print("import korgalore.cli OK on", sys.version.split()[0])'; then
        prerelease_failed="import"
    elif ! "$prereleaseenv/bin/kgl" --version; then
        prerelease_failed="cli"
    elif ! "$prereleaseenv/bin/python" -m pytest --durations=20; then
        prerelease_failed="pytest"
    fi

    prerelease_resolved=$("$prereleaseenv/bin/python" --version 2>&1 || echo "$PRERELEASE_PY (unresolved)")
    if [ -n "$prerelease_failed" ]; then
        printf '\nPrerelease lane failed on %s (advisory, not fatal): %s\n' \
            "$prerelease_resolved" "$prerelease_failed"
    else
        printf '\nPrerelease lane passed on %s\n' "$prerelease_resolved"
    fi
fi

if [ -n "$failed" ]; then
    printf '\nFAILURES:%s\n' "$failed"
    exit 1
fi

if [ -n "$PYTHONS" ]; then
    printf '\nAll interpreters passed: %s\n' "$PYTHONS"
fi
if [ -n "$FLOOR_PY" ]; then
    printf 'Floor lane passed on Python %s\n' "$FLOOR_PY"
fi
