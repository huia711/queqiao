#!/usr/bin/env bash
# Build 鹊桥.app and a drag-to-install DMG for Apple Silicon or Intel Macs.
#
#   macos/build.sh                 # version from the VERSION file, arch of this machine
#   ARCH=x86_64 macos/build.sh     # cross-build the Intel variant
#
# The app bundles a standalone CPython (python-build-standalone) with every Python dependency,
# so recipients only need TeX Live (optional, for arXiv source translation) and a Magpie /
# OpenAI-compatible endpoint.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${VERSION:-$(cat "$ROOT/VERSION")}"
ARCH="${ARCH:-$(uname -m)}"
case "$ARCH" in
  arm64|aarch64) PBS_ARCH=aarch64; SWIFT_TARGET=arm64-apple-macos13.0; ARCH=arm64 ;;
  x86_64) PBS_ARCH=x86_64; SWIFT_TARGET=x86_64-apple-macos13.0 ;;
  *) echo "unsupported ARCH=$ARCH" >&2; exit 1 ;;
esac
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
PBS_TAG="${PBS_TAG:-20261026}"
BUILD="$ROOT/build"
CACHE="$BUILD/cache"
APP="$BUILD/鹊桥.app"
DIST="$ROOT/dist"
DMG="$DIST/Queqiao-$VERSION-macOS-$ARCH.dmg"

echo "==> 鹊桥 $VERSION ($ARCH)"
rm -rf "$APP" "$BUILD/dmg"
mkdir -p "$CACHE" "$DIST" "$APP/Contents/MacOS" "$APP/Contents/Resources"

# 1. Standalone Python runtime --------------------------------------------------------------
PBS_FILE=$(curl -fsSL "https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/$PBS_TAG" \
  ${GITHUB_TOKEN:+-H "Authorization: Bearer $GITHUB_TOKEN"} |
  /usr/bin/python3 -c "import json,sys,re; d=json.load(sys.stdin); names=[a['name'] for a in d['assets'] if re.match(r'cpython-$PYTHON_VERSION\.\d+\+$PBS_TAG-$PBS_ARCH-apple-darwin-install_only_stripped\.tar\.gz$', a['name'])]; print(names[0])")
if [ ! -f "$CACHE/$PBS_FILE" ]; then
  echo "==> downloading $PBS_FILE"
  curl -fL --retry 3 -o "$CACHE/$PBS_FILE.part" "https://github.com/astral-sh/python-build-standalone/releases/download/$PBS_TAG/$PBS_FILE"
  mv "$CACHE/$PBS_FILE.part" "$CACHE/$PBS_FILE"
fi
tar -xzf "$CACHE/$PBS_FILE" -C "$APP/Contents/Resources"
mv "$APP/Contents/Resources/python" "$APP/Contents/Resources/runtime"
RUNTIME_PY="$APP/Contents/Resources/runtime/bin/python3"

# 2. Python dependencies --------------------------------------------------------------------
echo "==> installing dependencies"
PIP_PLATFORM=""
if [ "$ARCH" != "$(uname -m)" ]; then
  PIP_PLATFORM="--python-platform $PBS_ARCH-apple-darwin"
fi
# shellcheck disable=SC2086  # intentional word splitting of the optional flag
UV_HTTP_TIMEOUT=300 uv pip install --python "$RUNTIME_PY" --break-system-packages --no-cache --compile-bytecode \
  $PIP_PLATFORM -r "$ROOT/requirements.txt"

SITE=$(ls -d "$APP/Contents/Resources/runtime/lib/python$PYTHON_VERSION/site-packages")
# Trim things the app never uses at runtime.
rm -rf "$SITE"/ruff* "$SITE"/pip "$SITE"/pip-* "$APP/Contents/Resources/runtime/bin"/ruff \
  "$APP/Contents/Resources/runtime/lib/python$PYTHON_VERSION"/{idlelib,tkinter,turtledemo,ensurepip} \
  "$APP/Contents/Resources/runtime/lib"/{tcl*,tk*,itcl*,thread*} \
  "$APP/Contents/Resources/runtime/include" "$APP/Contents/Resources/runtime/share"
find "$APP/Contents/Resources/runtime" -type d -name tests -path '*site-packages/*' -prune -exec rm -rf {} + 2>/dev/null || true
find "$APP/Contents/Resources/runtime" -name '*.pyc' -path '*/test/*' -delete 2>/dev/null || true

# 3. Application code -----------------------------------------------------------------------
echo "==> copying application"
SERVER="$APP/Contents/Resources/server"
mkdir -p "$SERVER"
rsync -a --exclude '__pycache__' "$ROOT/app" "$ROOT/web" "$ROOT/glossary" "$ROOT/LICENSE" "$ROOT/VERSION" "$SERVER/"

# 4. Native wrapper -------------------------------------------------------------------------
echo "==> compiling wrapper"
swiftc -O -target "$SWIFT_TARGET" -o "$APP/Contents/MacOS/Queqiao" "$ROOT/macos/QueqiaoApp.swift"
BUILD_NUMBER="${BUILD_NUMBER:-$(date +%Y%m%d%H%M)}"
sed -e "s/__VERSION__/$VERSION/" -e "s/__BUILD__/$BUILD_NUMBER/" "$ROOT/macos/Info.plist" > "$APP/Contents/Info.plist"
printf 'APPL????' > "$APP/Contents/PkgInfo"

# 5. Icon ----------------------------------------------------------------------------------
echo "==> rendering icon"
ICONSET="$BUILD/AppIcon.iconset"
rm -rf "$ICONSET"
"$RUNTIME_PY" "$ROOT/macos/make_icon.py" "$ICONSET"
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

# 6. Smoke test (before signing; never write bytecode into the bundle) -----------------------
if [ "$ARCH" = "$(uname -m | sed 's/aarch64/arm64/')" ]; then
  echo "==> smoke test"
  (cd "$SERVER" && PYTHONDONTWRITEBYTECODE=1 QUEQIAO_DATA_DIR="$BUILD/smoke-data" "$RUNTIME_PY" -B -c "import app.main, app.pdf_worker, babeldoc, pymupdf; from bitstring import BitStream; print('imports ok')")
  rm -rf "$BUILD/smoke-data"
fi
find "$SERVER" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# 7. Ad-hoc signature (no Apple Developer ID: Gatekeeper will ask the user to confirm once) -----
echo "==> signing (ad-hoc)"
find "$APP/Contents/Resources/runtime" -type f \( -name '*.so' -o -name '*.dylib' \) -exec codesign --force --sign - --timestamp=none {} + >/dev/null 2>&1 || true
codesign --force --sign - --timestamp=none "$RUNTIME_PY" >/dev/null 2>&1 || true
codesign --force --deep --sign - --timestamp=none "$APP"
codesign --verify --deep --strict --verbose=1 "$APP"

# 8. DMG -----------------------------------------------------------------------------------
echo "==> building dmg"
STAGE="$BUILD/dmg"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
cp "$ROOT/macos/安装说明.txt" "$STAGE/"
rm -f "$DMG"
hdiutil create -volname "鹊桥 Queqiao $VERSION" -srcfolder "$STAGE" -fs HFS+ -format ULMO -ov "$DMG" >/dev/null
shasum -a 256 "$DMG" | tee "$DMG.sha256"
du -sh "$APP" "$DMG"
echo "==> done: $DMG"
