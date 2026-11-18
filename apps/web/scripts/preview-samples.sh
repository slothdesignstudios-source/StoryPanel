#!/usr/bin/env bash
# Local preview only: copy the sample comic pages into public/ so the landing page and gallery can show
# them. Permission to publish these pages is NOT recorded (src/content/samples.ts: approved: false), so
# public/samples/ is git-ignored and a production build never contains them.
#   scripts/preview-samples.sh          copy them in
#   scripts/preview-samples.sh --remove take them out again
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "${1:-}" = "--remove" ]; then rm -rf public/samples; echo "removed public/samples"; exit 0; fi
mkdir -p public/samples
cp preview-samples/*.webp public/samples/
echo "copied $(ls preview-samples/*.webp | wc -l) preview images into public/samples (not for publication)"
