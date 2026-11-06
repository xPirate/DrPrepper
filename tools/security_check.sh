#!/usr/bin/env bash
# Run before every release: known-vulnerable dependencies, static security lint, and the test suite.
#   pip install pip-audit bandit && tools/security_check.sh
set -euo pipefail
cd "$(dirname "$0")/.."
echo "== pip-audit (pinned dependencies)"
pip-audit -r constraints.txt --progress-spinner off
echo "== bandit (medium+ severity; SQL B608 hits are reviewed fixed column lists)"
bandit -q -r src -ll --skip B608,B104,B108
echo "== tests"
python -m pytest -q
echo "All checks passed."
