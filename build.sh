#!/bin/bash
set -e
cd "$(dirname "$0")"

APP_NAME="VoiceToText"
BUILD_DIR="build"
APP_BUNDLE="$BUILD_DIR/$APP_NAME.app"

echo "Building $APP_NAME..."
rm -rf "$BUILD_DIR"
mkdir -p "$APP_BUNDLE/Contents/MacOS"
mkdir -p "$APP_BUNDLE/Contents/Resources"

swiftc -O -o "$APP_BUNDLE/Contents/MacOS/$APP_NAME" \
    -framework Cocoa -framework AVFoundation -framework Carbon -framework CoreGraphics \
    main.swift

cp Info.plist "$APP_BUNDLE/Contents/"

# Code sign
codesign --force --sign - "$APP_BUNDLE" 2>/dev/null

# .env'yi config dizinine kopyala
mkdir -p ~/.config/voice-to-text
[ -f .env ] && cp .env ~/.config/voice-to-text/.env

echo "Build OK: $APP_BUNDLE"
