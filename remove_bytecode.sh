#!/usr/bin/env bash
set -euo pipefail

# Run from the repository root.
git rm -r --cached --ignore-unmatch __pycache__
git rm --cached --ignore-unmatch "*.pyc"
git rm --cached --ignore-unmatch "*.pyo"

git add .gitignore
git commit -m "chore: ignore and remove Python bytecode"
git push origin main
