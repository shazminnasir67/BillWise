#!/usr/bin/env bash
# Build the submission ZIP: output/ + source/ (+ demo/demo.mp4, .webm or .mov if present), then check its size and contents.
# Usage: scripts/package_zip.sh [zip_name] [templates_dir] [output_dir]
set -euo pipefail
cd "$(dirname "$0")/.."
ZIP="$(realpath -m "${1:-submission.zip}")"
TEMPLATES="${2:-data/test/csv}"
OUT="${3:-output}"  # folder holding level1.csv and level2.csv
for f in "$OUT/level1.csv" "$OUT/level2.csv" README.md requirements.txt .env.example; do
  [ -f "$f" ] || { echo "missing $f"; exit 1; }
done
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/output" "$STAGE/source"
cp "$OUT/level1.csv" "$OUT/level2.csv" "$STAGE/output/"
cp run.py README.md requirements.txt .env.example pytest.ini .gitignore "$STAGE/source/"
for d in core prompts scripts app frontend tests .streamlit; do
  [ -d "$d" ] && [ -n "$(ls -A "$d")" ] && cp -r "$d" "$STAGE/source/$d"
done
rm -rf "$STAGE/source/tests/golden"
find "$STAGE" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$STAGE" \( -name '.env' -o -name '*.pyc' \) -delete
for v in demo/demo.mp4 demo/demo.webm demo/demo.mov; do [ -f "$v" ] && mkdir -p "$STAGE/demo" && cp "$v" "$STAGE/demo/"; done
rm -f "$ZIP"
(cd "$STAGE" && zip -qr "$ZIP" .)
SIZE=$(stat -c %s "$ZIP")
echo "built $(basename "$ZIP"): $((SIZE / 1024)) KB"
[ "$SIZE" -le 15728640 ] || { echo "ZIP exceeds 15 MB"; exit 1; }
.venv/bin/python scripts/validate_submission.py --templates "$TEMPLATES" --output "$OUT" --zip "$ZIP"
