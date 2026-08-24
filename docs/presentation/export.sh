#!/usr/bin/env bash
# Regenerate slides/slide-NN.png from deck.html.
#
# Drives headless Chrome once per slide at 1600x900 with a 2x device scale
# factor, so each PNG is 3200x1800 and stays sharp on a 4K projector.
# `?export=1` strips the presenter affordances (progress bar, key hint).
set -euo pipefail
cd "$(dirname "$0")"

PORT=8901
SLIDES=19

# Chrome: prefer Playwright's bundled Chrome for Testing, fall back to a system
# install. A file:// URL will not work here (the deck reads location.search).
#
# NEWEST build wins. A plain `chromium-*` glob is sorted lexically, so it hands
# back chromium-1217 (Chrome 147) before chromium-1228 (Chrome 149), and older
# builds lay text out differently - same deck, visibly different slides. Pin the
# newest so a re-export is reproducible rather than dependent on which builds
# happen to be in the Playwright cache.
CHROME=""
newest=$(ls -d "$HOME/Library/Caches/ms-playwright"/chromium-* 2>/dev/null | sort -t- -k2 -n | tail -1)
for c in \
  "$newest/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing" \
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  "$(command -v google-chrome || true)" \
  "$(command -v chromium || true)"
do
  [ -n "$c" ] && [ -x "$c" ] && CHROME="$c" && break
done
[ -n "$CHROME" ] || { echo "no Chrome found - install Google Chrome or run 'npx playwright install chromium'"; exit 1; }
echo "renderer: $("$CHROME" --version 2>/dev/null)"

python3 -m http.server "$PORT" >/dev/null 2>&1 &
SERVER=$!
trap 'kill $SERVER 2>/dev/null || true' EXIT
sleep 1

mkdir -p slides
for n in $(seq 1 "$SLIDES"); do
  out=$(printf "slides/slide-%02d.png" "$n")
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars \
    --force-device-scale-factor=2 --window-size=1600,900 \
    --screenshot="$out" "http://127.0.0.1:$PORT/deck.html?export=1#$n" >/dev/null 2>&1
  printf "  %s\n" "$out"
done

echo "done - $SLIDES slides in slides/"
