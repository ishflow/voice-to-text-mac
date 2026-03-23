#!/bin/bash
set -e

cd "$(dirname "$0")"

APP_NAME="VoiceToText"
BUILD_DIR="build"
APP_BUNDLE="$BUILD_DIR/$APP_NAME.app"

echo "Building $APP_NAME..."

# Clean
rm -rf "$BUILD_DIR"
mkdir -p "$APP_BUNDLE/Contents/MacOS"
mkdir -p "$APP_BUNDLE/Contents/Resources"

# Compile
swiftc \
    -O \
    -o "$APP_BUNDLE/Contents/MacOS/$APP_NAME" \
    -framework Cocoa \
    -framework AVFoundation \
    -framework Carbon \
    -framework CoreGraphics \
    VoiceToText/main.swift

# Info.plist
cp VoiceToText/Info.plist "$APP_BUNDLE/Contents/"

echo ""
echo "Build OK: $APP_BUNDLE"
echo ""
echo "Kurmak icin:"
echo "  cp -r $APP_BUNDLE /Applications/"
echo ""
echo "Calistirmak icin:"
echo "  open /Applications/$APP_NAME.app"
