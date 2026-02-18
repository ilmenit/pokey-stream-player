#!/bin/bash
# ============================================================================
# POKEY Stream Player - Linux/macOS Build Script
# ============================================================================
# Builds standalone 'encode' executable using PyInstaller
#
# Requirements:
#   - Python 3.8+ installed
#   - Internet connection (for pip install on first run)
#
# Usage:
#   ./build.sh           - Build the executable
#   ./build.sh dist      - Build AND create distribution archive
#   ./build.sh clean     - Clean build directories
#   ./build.sh check     - Check dependencies only
#   ./build.sh install   - Install dependencies only
# ============================================================================

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Change to script directory
cd "$(dirname "$0")"

echo ""
echo "============================================================"
echo "  POKEY Stream Player - Build Script"
echo "============================================================"
echo ""

# Detect platform
detect_platform() {
    case "$(uname -s)" in
        Linux*)
            PLATFORM="linux"
            PLATFORM_DIR="linux_x86_64"
            DIST_SUFFIX="linux-x86_64"
            ;;
        Darwin*)
            PLATFORM="macos"
            if [[ "$(uname -m)" == "arm64" ]]; then
                PLATFORM_DIR="macos_aarch64"
                DIST_SUFFIX="macos-arm64"
            else
                PLATFORM_DIR="macos_x86_64"
                DIST_SUFFIX="macos-x86_64"
            fi
            ;;
        *)
            echo -e "${RED}[ERROR] Unsupported platform: $(uname -s)${NC}"
            exit 1
            ;;
    esac
    echo "Platform: $PLATFORM ($(uname -m))"
}

# Find Python
find_python() {
    if command -v python3 &> /dev/null; then
        PYTHON="python3"
    elif command -v python &> /dev/null; then
        PYTHON="python"
    else
        echo -e "${RED}[ERROR] Python not found${NC}"
        echo "Please install Python 3.8+ from https://python.org"
        exit 1
    fi

    PYVER=$($PYTHON --version 2>&1 | cut -d' ' -f2)
    echo "Python: $PYVER ($PYTHON)"
}

# Clean build directories
clean() {
    echo "Cleaning build directories..."
    rm -rf build dist release
    rm -f pokey-stream-player-*.tar.gz pokey-stream-player-*.zip
    find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    echo "Done."
}

# Check dependencies
check_deps() {
    echo ""
    echo "Checking required packages..."

    MISSING=""

    for pkg in numpy scipy soundfile PyInstaller; do
        if $PYTHON -c "import $pkg" 2>/dev/null; then
            echo -e "  ${GREEN}[OK]${NC} $pkg"
        else
            echo -e "  ${RED}[X]${NC} $pkg - MISSING"
            MISSING="$MISSING $pkg"
        fi
    done

    echo ""
    echo "Checking ASM templates..."
    if [ -f "asm/stream_player.asm" ]; then
        echo -e "  ${GREEN}[OK]${NC} asm/ directory found"
    else
        echo -e "  ${RED}[X]${NC} asm/ directory missing!"
        MISSING="$MISSING ASM"
    fi

    echo ""
    echo "Checking MADS (optional)..."
    MADS_PATH="bin/$PLATFORM_DIR/mads"
    if [ -f "$MADS_PATH" ]; then
        chmod +x "$MADS_PATH" 2>/dev/null || true
        echo -e "  ${GREEN}[OK]${NC} $MADS_PATH — will be included in dist"
    elif command -v mads &> /dev/null; then
        echo -e "  ${GREEN}[OK]${NC} mads found in PATH"
    else
        echo -e "  ${YELLOW}[?]${NC} mads not found — built-in assembler will be used"
        echo "      To include MADS: place the binary in bin/$PLATFORM_DIR/"
    fi

    echo ""
    echo "Checking FFmpeg (optional)..."
    if command -v ffmpeg &> /dev/null; then
        echo -e "  ${GREEN}[OK]${NC} ffmpeg found — MOD/XM/S3M/IT import enabled"
    else
        echo -e "  ${YELLOW}[?]${NC} ffmpeg not found — WAV/MP3/FLAC/OGG still work"
        echo "      Only needed for tracker formats (MOD, XM, S3M, IT)"
    fi

    echo ""
    if [ -n "$MISSING" ]; then
        echo "============================================================"
        echo -e "  ${RED}Missing components:${NC}$MISSING"
        echo "============================================================"
        echo ""
        echo "To install Python packages:"
        echo "  ./build.sh install"
        echo ""
        return 1
    fi

    echo "============================================================"
    echo -e "  ${GREEN}All checks passed!${NC}"
    echo "============================================================"
    return 0
}

# Install dependencies
install_deps() {
    echo ""
    echo "Installing dependencies..."
    $PYTHON -m pip install --upgrade pip
    $PYTHON -m pip install numpy scipy soundfile pyinstaller
    echo ""
    echo "Dependencies installed."
}

# Build executable
build() {
    echo ""
    echo "Installing/updating dependencies..."
    $PYTHON -m pip install --quiet --upgrade numpy scipy soundfile pyinstaller

    echo ""
    echo "Building standalone executable..."
    echo ""

    $PYTHON -m PyInstaller encode.spec --noconfirm --clean

    echo ""
    echo "============================================================"
    echo -e "  ${GREEN}BUILD SUCCESSFUL!${NC}"
    echo "============================================================"
    echo ""

    if [ -f "dist/encode" ]; then
        echo "  Output: $(pwd)/dist/encode"
        ls -lh "dist/encode" | awk '{print "  Size:   " $5}'
        echo ""
        echo "  To run:  ./dist/encode song.mp3"
        echo "  Help:    ./dist/encode -h"
        echo ""
        echo "  To create a distribution archive: ./build.sh dist"
    fi

    echo ""
}

# Build and create distribution
build_dist() {
    # First do the build
    build

    if [ ! -f "dist/encode" ]; then
        echo -e "${RED}[ERROR] encode binary not found${NC}"
        exit 1
    fi

    echo ""
    echo "Creating distribution..."
    echo ""

    RELDIR="release/pokey-stream-player"
    rm -rf release
    mkdir -p "$RELDIR"

    # Copy executable
    cp "dist/encode" "$RELDIR/"
    chmod +x "$RELDIR/encode"
    echo "  [+] encode"

    # Copy docs
    [ -f "README.md" ]         && cp "README.md"         "$RELDIR/" && echo "  [+] README.md"
    [ -f "project-design.md" ] && cp "project-design.md" "$RELDIR/" && echo "  [+] project-design.md"
    [ -f "LICENSE" ]           && cp "LICENSE"            "$RELDIR/" && echo "  [+] LICENSE"

    # Copy MADS if available
    MADS_PATH="bin/$PLATFORM_DIR/mads"
    if [ -f "$MADS_PATH" ]; then
        cp "$MADS_PATH" "$RELDIR/"
        chmod +x "$RELDIR/mads"
        echo "  [+] mads (external assembler)"
    else
        echo "  [?] mads not found — built-in assembler will be used"
    fi

    # Create archive
    ARCHIVE="pokey-stream-player-${DIST_SUFFIX}.tar.gz"
    tar -czf "$ARCHIVE" -C release pokey-stream-player
    ARCHIVE_SIZE=$(ls -lh "$ARCHIVE" | awk '{print $5}')

    echo ""
    echo "============================================================"
    echo -e "  ${GREEN}DISTRIBUTION READY${NC}"
    echo "============================================================"
    echo ""
    echo "  Folder:  $RELDIR/"
    echo "  Archive: $ARCHIVE ($ARCHIVE_SIZE)"
    echo ""
    echo "  Contents:"
    ls -1 "$RELDIR" | sed 's/^/    /'
    echo ""
    echo "  Usage:  ./encode song.mp3"
    echo "  Help:   ./encode -h"
    echo ""
    echo "  Optional: place 'mads' next to 'encode' for"
    echo "  external MADS assembly (otherwise uses built-in)."
    echo ""
}

# Main
detect_platform
find_python

case "${1:-build}" in
    clean)
        clean
        ;;
    check)
        check_deps || exit 1
        ;;
    install)
        install_deps
        ;;
    dist)
        if check_deps; then
            build_dist
        else
            echo ""
            echo "Please fix the issues above before building."
            exit 1
        fi
        ;;
    build|"")
        if check_deps; then
            build
        else
            echo ""
            echo "Please fix the issues above before building."
            exit 1
        fi
        ;;
    help|-h|--help)
        echo "Usage: $0 [command]"
        echo ""
        echo "Commands:"
        echo "  (none)    Build encode executable (default)"
        echo "  dist      Build AND create distribution archive"
        echo "  clean     Clean build directories"
        echo "  check     Check dependencies only"
        echo "  install   Install Python dependencies"
        echo "  help      Show this help"
        exit 0
        ;;
    *)
        echo "Unknown command: $1"
        echo "Usage: $0 [clean|check|install|build|dist|help]"
        exit 1
        ;;
esac
