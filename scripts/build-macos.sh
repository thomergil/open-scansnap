#!/bin/sh
# Build dist/<App>-<version>.dmg for this Mac's architecture.
#
#   SIGNING_IDENTITY  codesign identity; default: the login Keychain's
#                     "Developer ID Application" identity, else ad-hoc
#   NOTARY_KEY_PATH   App Store Connect API key (.p8); with NOTARY_KEY_ID and
#                     NOTARY_ISSUER_ID, the image is notarized and stapled
#   RELEASE=1         refuse to finish without a Developer ID identity and notarization
#   PYTHON            Python 3.13 to build with (default: Homebrew's python3.13)
#
# Needs SANE built into sane-prefix/ and Tesseract from Homebrew (see README).
# Python dependencies are wheels pinned by hash in requirements*.txt; nothing
# is built from source.
set -eu
cd "$(dirname "$0")/.."

PYTHON=${PYTHON:-$(brew --prefix python@3.13)/bin/python3.13}
B=build/macos
# Sparkle, for software updates; its release and published SHA-256
SPARKLE_VERSION=2.10.1
SPARKLE_SHA256=1a856d2a9edb8e628de88121d7be04cf0e2167f8641650b78fca00b14244cf27
SPARKLE_ACCOUNT=open-scansnap  # Keychain account of the update-signing key
CACHE=build/cache

for need in sane-prefix/bin/scanimage "$(brew --prefix tesseract)/bin/tesseract" "$PYTHON"; do
  [ -x "$need" ] || { echo "$need not found; see README (Install)." >&2; exit 1; }
done

rm -rf "$B"
mkdir -p "$B" dist "$CACHE"
SPARKLE_ARCHIVE=$CACHE/Sparkle-$SPARKLE_VERSION.tar.xz
[ -f "$SPARKLE_ARCHIVE" ] || curl -fsSL -o "$SPARKLE_ARCHIVE" \
  "https://github.com/sparkle-project/Sparkle/releases/download/$SPARKLE_VERSION/Sparkle-$SPARKLE_VERSION.tar.xz"
echo "$SPARKLE_SHA256  $SPARKLE_ARCHIVE" | shasum -a 256 -c - >/dev/null
SPARKLE=$B/sparkle
mkdir -p "$SPARKLE"
tar -xJf "$SPARKLE_ARCHIVE" -C "$SPARKLE"

# Tesseract's language data besides English: each file of
# packaging/macos/languages.txt, from tessdata_fast at its commit, checked
LANGUAGE_LIST=packaging/macos/languages.txt
TESSDATA_COMMIT=$(sed -n 's/^# commit //p' "$LANGUAGE_LIST")
LANGUAGE_DIR=$PWD/$CACHE/tessdata-$TESSDATA_COMMIT
mkdir -p "$LANGUAGE_DIR"
grep -v '^#' "$LANGUAGE_LIST" | while read -r _ name; do
  [ -f "$LANGUAGE_DIR/$name" ] || curl -fsSL -o "$LANGUAGE_DIR/$name" \
    "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/$TESSDATA_COMMIT/$name"
done
(cd "$LANGUAGE_DIR" && grep -v '^#' "$OLDPWD/$LANGUAGE_LIST" | shasum -a 256 -c - >/dev/null)
"$PYTHON" -m venv "$B/venv"
VPY=$B/venv/bin/python
"$VPY" -m pip install -q --require-hashes --only-binary :all: -r requirements.txt -r requirements-dev.txt

APP_NAME=$("$VPY" -c 'from openscansnap import APP_NAME; print(APP_NAME)')
VERSION=$("$VPY" -c 'from openscansnap import __version__; print(__version__)')
APP=$B/dist/$APP_NAME.app
DMG=dist/$(echo "$APP_NAME" | tr -d ' ')-$VERSION.dmg
rm -f "$DMG" "$DMG.sha256"

if [ -z "${SIGNING_IDENTITY:-}" ]; then
  SIGNING_IDENTITY=$(security find-identity -v -p codesigning \
    | awk -F'"' '/Developer ID Application/ { print $2; exit }')
fi
SIGNING_IDENTITY=${SIGNING_IDENTITY:--}
if [ "${RELEASE:-}" = 1 ] && { [ "$SIGNING_IDENTITY" = - ] || [ -z "${NOTARY_KEY_PATH:-}" ]; }; then
  echo "RELEASE=1 needs a Developer ID Application identity and NOTARY_KEY_PATH, NOTARY_KEY_ID, NOTARY_ISSUER_ID." >&2
  exit 1
fi

TESSERACT_PREFIX=$(brew --prefix tesseract)
LICENSES=$PWD/$B/Licenses
"$VPY" packaging/macos/licenses.py "$LICENSES" --other "Sparkle=$SPARKLE/LICENSE" \
  --other "tessdata_fast=$LANGUAGE_DIR/LICENSE" \
  sane-prefix/bin/scanimage sane-prefix/lib/libsane.1.dylib \
  sane-prefix/lib/sane/libsane-fujitsu.1.so "$TESSERACT_PREFIX/bin/tesseract"
ICON=$PWD/$B/$APP_NAME.icns
"$VPY" packaging/macos/icon.py "$ICON"
ICON=$ICON LICENSES=$LICENSES TESSERACT_PREFIX=$TESSERACT_PREFIX LANGUAGE_DIR=$LANGUAGE_DIR \
  "$B/venv/bin/pyinstaller" --noconfirm --clean --log-level WARN \
  --distpath "$B/dist" --workpath "$B/work" packaging/macos/OpenScanSnap.spec
"$VPY" packaging/macos/fix_links.py "$APP"
ditto "$SPARKLE/Sparkle.framework" "$APP/Contents/Frameworks/Sparkle.framework"

if [ "$SIGNING_IDENTITY" = - ]; then
  echo "No Developer ID identity: signing ad-hoc." >&2
  codesign --force --deep --sign - "$APP"
else
  codesign --force --deep --options runtime --timestamp \
    --entitlements packaging/macos/entitlements.plist --sign "$SIGNING_IDENTITY" "$APP"
fi
codesign --verify --deep --strict "$APP"
# The signed app must load its own programs (the hardened runtime checks
# every library) and turn a page into a searchable PDF with them
"$APP/Contents/Frameworks/sane-prefix/bin/scanimage" -V >/dev/null
"$APP/Contents/MacOS/$APP_NAME" --self-test

mkdir -p "$B/dmg"
cp -R "$APP" "$B/dmg/"
ln -s /Applications "$B/dmg/Applications"
hdiutil create -quiet -volname "$APP_NAME" -srcfolder "$B/dmg" -format UDZO "$DMG"

if [ "$SIGNING_IDENTITY" != - ]; then
  codesign --force --timestamp --sign "$SIGNING_IDENTITY" "$DMG"
  if [ -n "${NOTARY_KEY_PATH:-}" ]; then
    xcrun notarytool submit "$DMG" --key "$NOTARY_KEY_PATH" \
      --key-id "${NOTARY_KEY_ID:?}" --issuer "${NOTARY_ISSUER_ID:?}" --wait
    xcrun stapler staple "$DMG"
    xcrun stapler validate "$DMG"
  fi
fi
(cd dist && shasum -a 256 "$(basename "$DMG")" > "$(basename "$DMG").sha256")
# The update feed for this release, signed with the Keychain key; published
# as a release asset, it is what SUFeedURL finds
APPCAST_DIR=$B/appcast
mkdir -p "$APPCAST_DIR"
cp "$DMG" "$APPCAST_DIR/"
"$SPARKLE/bin/generate_appcast" --account "$SPARKLE_ACCOUNT" \
  --download-url-prefix "https://github.com/thomergil/open-scansnap/releases/download/v$VERSION/" \
  "$APPCAST_DIR"
cp "$APPCAST_DIR/appcast.xml" dist/appcast.xml

# Leave only the image: macOS mixes up apps with several copies installed
# (TN3179, FB15568200), and LaunchServices registers these build copies.
LSREGISTER=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
"$LSREGISTER" -u "$APP" "$B/dmg/$APP_NAME.app" 2>/dev/null || true
rm -rf "$B/dist" "$B/dmg"
echo "$DMG"
