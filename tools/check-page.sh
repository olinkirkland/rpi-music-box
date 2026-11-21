#!/usr/bin/env bash
# Check the web page without a browser: JS syntax, Vue templates, handlers.
# Usage: tools/check-page.sh   (from anywhere; needs node, e.g. via nvm)
set -euo pipefail
cd "$(dirname "$0")/.."
STATIC=musicbox/static
DEPS="${XDG_CACHE_HOME:-$HOME/.cache}/tap-to-play-dev"
if [ -s "$HOME/.nvm/nvm.sh" ]; then source "$HOME/.nvm/nvm.sh" >/dev/null; fi
if [ ! -d "$DEPS/node_modules/@vue/compiler-dom" ]; then
  echo "installing @vue/compiler-dom@3.5.13 into $DEPS (once)"
  npm install --silent --no-audit --no-fund --prefix "$DEPS" @vue/compiler-dom@3.5.13
fi
JS=("$STATIC/app.js" "$STATIC"/components/*.js)
for f in "${JS[@]}"; do node --check "$f"; done
echo "JS syntax: ok (${#JS[@]} files)"
node tools/check-page.mjs "$DEPS/node_modules" "$STATIC/index.html" "${JS[@]}"
python3 tools/check_handlers.py "$STATIC/index.html" "${JS[@]}"
