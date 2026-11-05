#!/usr/bin/env bash
# Build 鹊桥屏译.app (menu-bar screen translator) into lens/build/.
#   lens/build.sh            build
#   lens/build.sh install    build and copy to /Applications
set -euo pipefail
cd "$(dirname "$0")"

APP="build/鹊桥屏译.app"
BIN="$APP/Contents/MacOS/QueqiaoLens"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

swiftc -O -parse-as-library -target "$(uname -m)-apple-macos14.0" \
  -o "$BIN" Sources/*.swift

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>鹊桥屏译</string>
  <key>CFBundleDisplayName</key><string>鹊桥屏译</string>
  <key>CFBundleIdentifier</key><string>io.github.huia711.queqiao-lens</string>
  <key>CFBundleExecutable</key><string>QueqiaoLens</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/><key>NSAllowsArbitraryLoads</key><true/></dict>
</dict>
</plist>
PLIST

ICONSET=../build/AppIcon.iconset
if [ -d "$ICONSET" ]; then
  iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"
fi

# Sign with the Apple Development certificate when there is one, so the screen-recording permission
# survives rebuilds; otherwise fall back to an ad-hoc signature.
IDENTITY=$(security find-identity -v -p codesigning | sed -n 's/.*"\(Apple Development: [^"]*\)".*/\1/p' | head -1)
codesign --force --deep --sign "${IDENTITY:--}" "$APP"
if [ -n "$IDENTITY" ]; then SIGNED="Apple Development"; else SIGNED="ad-hoc"; fi
echo "built $APP (signed: $SIGNED)"

if [ "${1:-}" = "install" ]; then
  pkill -x QueqiaoLens 2>/dev/null || true
  ditto "$APP" "/Applications/鹊桥屏译.app"
  echo "installed /Applications/鹊桥屏译.app"
fi
