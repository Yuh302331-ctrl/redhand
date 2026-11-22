#!/bin/bash
set -euo pipefail

if [ "$#" -ne 4 ]; then
    printf 'Usage: bash %s AGENTTRUEUP_SOURCE PYINSTALLER_PYTHON BUILD_DIR OUTPUT_APP\n' "$0" >&2
    exit 2
fi

desktop=$(cd "$(dirname "$0")" && pwd)
agenttrueup_source=$(cd "$1" && pwd)
python=$2
build_dir=$3
app=$4
if [ -e "$app" ]; then
    printf 'Output already exists: %s\n' "$app" >&2
    exit 1
fi

mkdir -p "$build_dir" "$app/Contents/MacOS" "$app/Contents/Resources"
architecture=$(uname -m)
sdk=$(xcrun --sdk macosx --show-sdk-path)
xcrun swiftc -swift-version 5 -O -parse-as-library -sdk "$sdk" \
    -target "$architecture-apple-macosx14.0" -o "$app/Contents/MacOS/ChangeDesk" "$desktop/App.swift"

"$python" -m PyInstaller --noconfirm --onedir --name changes-engine \
    --paths "$desktop/../src" --paths "$agenttrueup_source/src" \
    --specpath "$build_dir" --workpath "$build_dir/pyinstaller" --distpath "$build_dir/dist" \
    "$desktop/engine.py"
ditto "$build_dir/dist/changes-engine" "$app/Contents/Resources/engine"
mkdir -p "$app/Contents/Resources/Licenses"
cp "$desktop/../LICENSE" "$app/Contents/Resources/Licenses/redhand.txt"
cp "$agenttrueup_source/LICENSE" "$app/Contents/Resources/Licenses/agenttrueup.txt"

cat > "$app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key><string>zh-Hans</string>
    <key>CFBundleDisplayName</key><string>文件改动记录</string>
    <key>CFBundleExecutable</key><string>ChangeDesk</string>
    <key>CFBundleIdentifier</key><string>local.redhand.ChangeDesk</string>
    <key>CFBundleName</key><string>文件改动记录</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>0.1.0</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>LSApplicationCategoryType</key><string>public.app-category.utilities</string>
    <key>LSMinimumSystemVersion</key><string>14.0</string>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

codesign --force --deep --sign - "$app"
codesign --verify --deep --strict "$app"
