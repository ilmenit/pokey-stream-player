# -*- mode: python ; coding: utf-8 -*-
# ============================================================================
# PyInstaller spec for POKEY Stream Player (encode CLI)
#
# Produces a single-file executable: encode (Linux/macOS) or encode.exe (Win)
#
# Usage:
#   python -m PyInstaller encode.spec --noconfirm --clean
#
# Or use the build scripts:
#   build.bat           (Windows)
#   ./build.sh          (Linux/macOS)
# ============================================================================

import platform

block_cipher = None
IS_WINDOWS = platform.system() == 'Windows'

# ── Data files bundled into the executable ────────────────────────────
# The asm/ folder contains assembly templates needed at runtime.
# Inside the frozen exe they appear under sys._MEIPASS/asm/

datas = [
    ('asm', 'asm'),
]

# ── Hidden imports PyInstaller may miss ───────────────────────────────
# soundfile wraps libsndfile via cffi; scipy submodules are lazy-loaded.

hiddenimports = [
    'soundfile',
    'numpy',
    'numpy.testing',
    'scipy.signal',
    'scipy.fft',
    'scipy.interpolate',
    'scipy._lib',
    'scipy._lib.array_api_compat',
    'scipy._lib.array_api_compat.numpy',
]

# ── Analysis ──────────────────────────────────────────────────────────

a = Analysis(
    ['encode_entry.py'],
    pathex=['src'],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Only exclude things we're 100% sure nothing pulls in
        'tkinter',
        'matplotlib', 'PIL', 'IPython', 'jupyter',
        'pytest', 'setuptools', 'pip',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# ── Trim unused scipy submodules to reduce size (~30-50 MB saved) ─────

_SCIPY_EXCLUDE = [
    'scipy.linalg', 'scipy.optimize', 'scipy.sparse',
    'scipy.stats', 'scipy.spatial', 'scipy.integrate',
    'scipy.ndimage', 'scipy.special', 'scipy.cluster',
    'scipy.io', 'scipy.odr', 'scipy.misc',
]
a.binaries = [b for b in a.binaries
              if not any(x in b[0] for x in _SCIPY_EXCLUDE)]

# ── Package ───────────────────────────────────────────────────────────

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='encode',
    debug=False,
    bootloader_ignore_signals=False,
    strip=not IS_WINDOWS,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
