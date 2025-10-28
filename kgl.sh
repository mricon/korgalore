#!/usr/bin/env bash
#
# Run from a git checkout.
#

REAL_SCRIPT=$(realpath -e ${BASH_SOURCE[0]})
SCRIPT_TOP="${SCRIPT_TOP:-$(dirname ${REAL_SCRIPT})}"

PYTHONPATH="${SCRIPT_TOP}/src${PYTHONPATH:+:$PYTHONPATH}" \
	exec python3 "${SCRIPT_TOP}/src/korgalore/cli.py" "${@}"
