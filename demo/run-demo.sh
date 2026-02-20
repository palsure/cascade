#!/usr/bin/env bash
#
# Cascade Demo -- One-command demo script
#
# Prerequisites:
#   1. npm install -g cline
#   2. cline auth  (configure your API provider)
#   3. pip install -r requirements.txt
#
# Usage:
#   cd cline/
#   bash demo/run-demo.sh
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
DEMO_DIR="$SCRIPT_DIR"

echo ""
echo "   ____                        _"
echo "  / ___|__ _ ___  ___ __ _  __| | ___"
echo " | |   / _\` / __|/ __/ _\` |/ _\` |/ _ \\"
echo " | |__| (_| \\__ \\ (_| (_| | (_| |  __/"
echo "  \\____\\__,_|___/\\___\\__,_|\\__,_|\\___|"
echo "  Multi-Repo Change Propagator · Cline CLI as Infrastructure"
echo ""

# Check prerequisites
echo "[1/4] Checking prerequisites..."

if ! command -v cline &>/dev/null; then
    echo "  ERROR: cline CLI not found. Install with: npm install -g cline"
    echo "  Then run: cline auth"
    exit 1
fi
echo "  cline CLI: $(which cline)"

if ! command -v python3 &>/dev/null; then
    echo "  ERROR: python3 not found"
    exit 1
fi
echo "  python3: $(python3 --version)"

if ! command -v git &>/dev/null; then
    echo "  ERROR: git not found"
    exit 1
fi
echo "  git: $(git --version)"

echo ""

# Initialize git repos for each demo repo
echo "[2/4] Initializing demo repos as git repositories..."

for repo in backend-api web-dashboard python-sdk cli-client; do
    REPO_PATH="$DEMO_DIR/repos/$repo"
    if [ ! -d "$REPO_PATH/.git" ]; then
        echo "  Initializing $repo..."
        (cd "$REPO_PATH" && git init -q && git add -A && git commit -q -m "initial commit")
    else
        echo "  $repo already initialized"
    fi
done

echo ""

# Run pre-change tests to show they pass
echo "[3/4] Running pre-change tests to verify baseline..."

(cd "$DEMO_DIR/repos/python-sdk" && python3 -m pytest test_sdk.py -v 2>&1) || true
(cd "$DEMO_DIR/repos/cli-client" && python3 -m pytest test_cli.py -v 2>&1) || true
(cd "$DEMO_DIR/repos/web-dashboard" && node tests/test.js 2>&1) || true

echo ""

# Run Cascade
echo "[4/4] Running Cascade propagation..."
echo ""
echo "  Change: 'The /users endpoint returns full_name instead of first_name and last_name'"
echo ""

cd "$PROJECT_DIR"
python3 -m cascade run \
    --config "$DEMO_DIR/cascade.yaml" \
    "The /users endpoint now returns full_name (a single string) instead of separate first_name and last_name fields. The /posts endpoint returns author_name instead of author_first_name and author_last_name. Update all models, API calls, display logic, and tests accordingly."

echo ""
echo "Demo complete! Check the branches created in each repo:"
for repo in backend-api web-dashboard python-sdk cli-client; do
    echo "  $DEMO_DIR/repos/$repo:"
    (cd "$DEMO_DIR/repos/$repo" && git branch 2>/dev/null | head -5)
done
