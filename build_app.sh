#!/usr/bin/env zsh
# build_app.sh — Build AudioToClaude.app, install locally, and produce a DMG
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR"

# ── 1. Activate venv ────────────────────────────────────────────────────────
source .venv/bin/activate

# ── 2. Install PyInstaller into the venv if needed ──────────────────────────
if ! python -c "import PyInstaller" &>/dev/null; then
    echo "Installing PyInstaller…"
    pip install pyinstaller
fi

# ── 3. Clean previous build artefacts ───────────────────────────────────────
rm -rf build dist

# ── 4. Build the .app bundle ────────────────────────────────────────────────
echo "Building AudioToClaude.app…"
pyinstaller AudioToClaude.spec

# ── 5. Copy .env to ~/Library/Application Support/audio-to-claude/ ──────────
APP_SUPPORT="$HOME/Library/Application Support/audio-to-claude"
mkdir -p "$APP_SUPPORT"
if [[ -f .env ]]; then
    cp .env "$APP_SUPPORT/.env"
    echo "Copied .env → $APP_SUPPORT/.env"
fi

# ── 6. Install to /Applications ─────────────────────────────────────────────
echo "Installing to /Applications…"
rm -rf /Applications/AudioToClaude.app
cp -R dist/AudioToClaude.app /Applications/AudioToClaude.app

# ── 7. Build distributable DMG ──────────────────────────────────────────────
echo ""
echo "Building AudioToClaude.dmg…"
rm -f dist/AudioToClaude.dmg

if command -v create-dmg &>/dev/null; then
    # Preferred: create-dmg produces a polished installer DMG
    create-dmg \
        --volname "AudioToClaude" \
        --volicon "assets/icon.icns" \
        --window-pos 200 150 \
        --window-size 560 340 \
        --icon-size 100 \
        --icon "AudioToClaude.app" 140 170 \
        --hide-extension "AudioToClaude.app" \
        --app-drop-link 420 170 \
        "dist/AudioToClaude.dmg" \
        "dist/AudioToClaude.app"
else
    # Fallback: plain hdiutil (no Homebrew required)
    echo "(create-dmg not found, using hdiutil — install via 'brew install create-dmg' for a nicer DMG)"
    _staging="$(mktemp -d)"
    cp -R dist/AudioToClaude.app "$_staging/"
    ln -sf /Applications "$_staging/Applications"
    hdiutil create \
        -volname "AudioToClaude" \
        -srcfolder "$_staging" \
        -ov -format UDZO \
        dist/AudioToClaude.dmg
    rm -rf "$_staging"
fi

echo ""
echo "Done!"
echo "  App   → /Applications/AudioToClaude.app"
echo "  DMG   → $SCRIPT_DIR/dist/AudioToClaude.dmg"
echo ""
echo "Share dist/AudioToClaude.dmg with your friend."
echo "They will need to right-click → Open on first launch (unsigned app)."
