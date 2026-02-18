"""Generate assembly project for all compression modes.

Produces a self-contained project directory with:
  - Static .asm/.inc files (copied from package's asm/ directory)
  - Generated data files (config, tables, bank data, splash text)

Assembly is handled by the built-in simple_mads assembler.
The project is also compatible with the external MADS assembler:
    mads stream_player.asm -o:output.xex
"""

import os
import shutil
import sys

from .tables import index_to_volumes, max_level
from .layout import DBANK_TABLE, TAB_MEM_BANKS
from .splash_utils import to_screen_codes, format_info_line


# Static .asm files shipped with the package (relative to asm/ directory)
STATIC_FILES = [
    'atari.inc',
    'copy_rom.asm',
    'mem_detect.asm',
    'splash.asm',
    'pokey_setup.asm',
    'stream_player.asm',
    # Mode-specific (all copied; master uses conditional assembly)
    'zeropage_vq.inc',
    'zeropage_lz.inc',
    'zeropage_raw.inc',
    'player_vq.asm',
    'player_lz.asm',
    'player_raw.asm',
    'irq_vq.asm',
    'irq_lz.asm',
    'irq_raw.asm',
]

# Mode constants (must match stream_player.asm)
MODE_RAW = 0
MODE_LZ = 1
MODE_VQ = 2

BANK_BASE = 0x4000


def _normalize_asm_dir():
    """Find the asm/ directory, trying multiple locations.

    Search order:
      1. PyInstaller bundle (sys._MEIPASS/asm/)
      2. Next to the executable (for frozen one-dir builds)
      3. Relative to this source file (development layout)
    """
    candidates = []

    # PyInstaller onefile: extracted to temp dir
    if hasattr(sys, '_MEIPASS'):
        candidates.append(os.path.join(sys._MEIPASS, 'asm'))

    # Next to the running executable (frozen one-dir or user placement)
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    candidates.append(os.path.join(exe_dir, 'asm'))

    # Development layout: src/stream_player/../../asm → project_root/asm
    pkg_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates.append(os.path.normpath(os.path.join(pkg_dir, '..', 'asm')))
    candidates.append(os.path.join(pkg_dir, 'asm'))
    candidates.append(os.path.normpath(os.path.join(pkg_dir, '..', '..', 'asm')))

    for c in candidates:
        if os.path.isdir(c):
            return c

    raise FileNotFoundError(
        "Cannot find asm/ directory. Searched:\n  " +
        "\n  ".join(candidates))


def generate_project(output_dir, banks, compress_mode, divisor, audctl,
                     actual_rate, pokey_channels=2, vec_size=4,
                     source_name='', duration=0.0, stereo=False):
    """Generate complete assembly project.

    Args:
        output_dir: Directory to write project files
        banks: List of bank data (bytes), each up to 16384 bytes
        compress_mode: 'vq', 'lz', or 'raw'
        divisor: POKEY timer divisor byte
        audctl: AUDCTL register value
        actual_rate: Actual sample rate in Hz
        pokey_channels: Number of POKEY channels (1-4)
        vec_size: VQ vector size (2/4/8/16, only used for VQ mode)
        source_name: Original audio filename (for comments)
        duration: Audio duration in seconds
        stereo: True for dual-POKEY stereo

    Returns:
        Path to master stream_player.asm file.
    """
    os.makedirs(output_dir, exist_ok=True)
    n_banks = len(banks)

    mode_int = {'raw': MODE_RAW, 'lz': MODE_LZ, 'vq': MODE_VQ}[compress_mode]

    # 1. Copy static .asm files
    asm_src = _normalize_asm_dir()
    for fname in STATIC_FILES:
        src = os.path.join(asm_src, fname)
        dst = os.path.join(output_dir, fname)
        if os.path.exists(src):
            shutil.copy2(src, dst)

    # 2. Generate per-song data files
    _write_config(output_dir, n_banks, mode_int, divisor, audctl,
                  actual_rate, pokey_channels, vec_size, source_name,
                  duration, stereo)
    _write_audc_tables(output_dir, pokey_channels)
    _write_portb_table(output_dir)
    _write_splash_data(output_dir, pokey_channels, actual_rate,
                       compress_mode, vec_size, n_banks)
    _write_banks_asm(output_dir, n_banks)

    # 3. Write bank data files
    for i, bank_data in enumerate(banks):
        _write_bank_data(output_dir, i, bank_data)

    # 4. VQ-specific tables
    if compress_mode == 'vq':
        _write_vq_tables(output_dir, vec_size)

    return os.path.join(output_dir, 'stream_player.asm')


# ══════════════════════════════════════════════════════════════════════
# Generated file writers
# ══════════════════════════════════════════════════════════════════════

def _write_config(output_dir, n_banks, mode_int, divisor, audctl,
                  actual_rate, pokey_channels, vec_size, source_name,
                  duration, stereo):
    """Write config.asm with per-song constants."""
    mode_names = {MODE_RAW: 'RAW', MODE_LZ: 'DeltaLZ', MODE_VQ: 'VQ'}
    clk = '1.77MHz ch1' if (audctl & 0x40) else '64kHz base'
    dur_m = int(duration) // 60
    dur_s = int(duration) % 60

    lines = [
        '; ==========================================================================',
        '; config.asm -- Per-song constants (generated by Python encoder)',
        '; ==========================================================================',
        f'; Source: {source_name}',
        f'; Mode: {mode_names[mode_int]}, {pokey_channels}ch'
        f'{", stereo" if stereo else ""}',
        f'; Rate: {actual_rate:.1f} Hz (divisor ${divisor:02X}, {clk})',
        f'; Duration: {dur_m}:{dur_s:02d}',
        f'; Banks: {n_banks}',
        '; ==========================================================================',
        '',
        '; --- Compression mode ---',
        f'COMPRESS_MODE   = {mode_int}'
        f'       ; {mode_names[mode_int]}',
        '',
        '; --- Player configuration ---',
        f'N_BANKS         = {n_banks}',
        f'POKEY_CHANNELS  = {pokey_channels}',
        f'POKEY_DIVISOR   = ${divisor:02X}'
        f'       ; -> {actual_rate:.1f} Hz',
        f'AUDCTL_VAL      = ${audctl:02X}'
        f'       ; {clk}',
    ]

    if mode_int == MODE_VQ:
        lines.extend([
            '',
            '; --- VQ parameters ---',
            f'VEC_SIZE        = {vec_size}',
        ])

    path = os.path.join(output_dir, 'config.asm')
    with open(path, 'w', newline='\n') as f:
        f.write('\n'.join(lines) + '\n')


def _write_audc_tables(output_dir, pokey_channels):
    """Write AUDC lookup tables (index → AUDC register value)."""
    max_lvl = max_level(pokey_channels)

    lines = [
        '; ==========================================================================',
        '; audc_tables.asm -- AUDC lookup tables (generated)',
        '; ==========================================================================',
        f'; {pokey_channels} channel(s), {max_lvl + 1} levels (0-{max_lvl})',
        f'; Each table: index -> volume | $10 (volume-only mode), padded to 256',
        '',
    ]

    for ch in range(pokey_channels):
        lines.append(f'audc{ch+1}_tab:')
        tab = []
        for idx in range(max_lvl + 1):
            vols = index_to_volumes(idx, pokey_channels)
            tab.append(vols[ch] | 0x10)
        # Pad to 256 entries
        tab += [0x10] * (256 - len(tab))
        for i in range(0, 256, 16):
            vals = ','.join(f'${v:02X}' for v in tab[i:i+16])
            lines.append(f'    .byte {vals}')
        lines.append('')

    path = os.path.join(output_dir, 'audc_tables.asm')
    with open(path, 'w', newline='\n') as f:
        f.write('\n'.join(lines) + '\n')


def _write_portb_table(output_dir):
    """Write PORTB table placeholder (filled at runtime by play_init)."""
    lines = [
        '; ==========================================================================',
        '; portb_table.asm -- Bank PORTB values (patched at runtime)',
        '; ==========================================================================',
        '; play_init copies detected values from TAB_MEM_BANKS into this table.',
        '',
        'portb_table:',
    ]
    for i in range(0, 64, 16):
        vals = ','.join(['$FE'] * 16)
        lines.append(f'    .byte {vals}')

    path = os.path.join(output_dir, 'portb_table.asm')
    with open(path, 'w', newline='\n') as f:
        f.write('\n'.join(lines) + '\n')


def _write_vq_tables(output_dir, vec_size):
    """Write VQ codebook address lookup tables."""
    lines = [
        '; ==========================================================================',
        '; vq_tables.asm -- VQ codebook address lookup (generated)',
        '; ==========================================================================',
        f'; vec_size={vec_size}: codebook at $4000, '
        f'each entry = {vec_size} bytes',
        f'; index N -> address $4000 + N * {vec_size}',
        '',
        'vq_lo_tab:',
    ]
    for i in range(0, 256, 16):
        vals = ','.join(f'${(BANK_BASE + j * vec_size) & 0xFF:02X}'
                        for j in range(i, i + 16))
        lines.append(f'    .byte {vals}')

    lines.extend(['', 'vq_hi_tab:'])
    for i in range(0, 256, 16):
        vals = ','.join(f'${((BANK_BASE + j * vec_size) >> 8) & 0xFF:02X}'
                        for j in range(i, i + 16))
        lines.append(f'    .byte {vals}')

    path = os.path.join(output_dir, 'vq_tables.asm')
    with open(path, 'w', newline='\n') as f:
        f.write('\n'.join(lines) + '\n')


def _write_splash_data(output_dir, pokey_channels, actual_rate,
                       compress_mode, vec_size, n_banks):
    """Write splash screen text as ANTIC Mode 2 screen codes."""
    ram_kb = n_banks * 16 + 64

    line1 = "  STREAM PLAYER  -  [SPACE] TO PLAY     "
    line2 = format_info_line(pokey_channels, actual_rate, compress_mode,
                             vec_size, ram_kb)
    err_title = "STREAM PLAYER".center(40)
    err_msg = f"ERROR: {ram_kb}KB MEMORY REQUIRED".center(40)

    def _fmt_codes(label, text):
        codes = to_screen_codes(text)
        result = [f'{label}:']
        for i in range(0, 40, 8):
            vals = ','.join(f'${c:02X}' for c in codes[i:i+8])
            result.append(f'    .byte {vals}')
        return result

    lines = [
        '; ==========================================================================',
        '; splash_data.asm -- Splash screen text (generated)',
        '; ==========================================================================',
        '; 40 bytes per line, ANTIC Mode 2 screen codes.',
        '',
    ]
    lines.extend(_fmt_codes('text_line1', line1))
    lines.append('')
    lines.extend(_fmt_codes('text_line2', line2))
    lines.append('')
    lines.extend(_fmt_codes('text_err_title', err_title))
    lines.append('')
    lines.extend(_fmt_codes('text_err_msg', err_msg))

    path = os.path.join(output_dir, 'splash_data.asm')
    with open(path, 'w', newline='\n') as f:
        f.write('\n'.join(lines) + '\n')


def _write_bank_data(output_dir, bank_idx, data):
    """Write one bank's data as .byte directives."""
    lines = [
        '; ==========================================================================',
        f'; bank_{bank_idx:02d}.asm -- Bank {bank_idx} data'
        f' ({len(data)} bytes, generated)',
        '; ==========================================================================',
        '',
        f'    org BANK_BASE',
        '',
    ]
    for i in range(0, len(data), 16):
        chunk = data[i:i+16]
        vals = ','.join(f'${b:02X}' for b in chunk)
        lines.append(f'    .byte {vals}')

    path = os.path.join(output_dir, f'bank_{bank_idx:02d}.asm')
    with open(path, 'w', newline='\n') as f:
        f.write('\n'.join(lines) + '\n')


def _write_banks_asm(output_dir, n_banks):
    """Write bank loading stubs (INI segments for XEX loading)."""
    lines = [
        '; ==========================================================================',
        '; banks.asm -- Bank loading INI stubs (generated)',
        '; ==========================================================================',
        '; Each bank: INI stub switches PORTB to target bank, includes data at $4000,',
        '; then INI stub switches PORTB back to main RAM.',
        '; Uses STUB_ADDR ($0600) area for the switch routines.',
        '',
    ]

    for i in range(n_banks):
        lines.extend([
            f'; --- Bank {i} ---',
            '    org STUB_ADDR',
            f'    lda TAB_MEM_BANKS+{i+1}',
            '    sta PORTB',
            '    rts',
            '    ini STUB_ADDR',
            '',
            f"    icl 'bank_{i:02d}.asm'",
            '',
            '    org STUB_ADDR',
            '    lda #PORTB_MAIN',
            '    sta PORTB',
            '    rts',
            '    ini STUB_ADDR',
            '',
        ])

    path = os.path.join(output_dir, 'banks.asm')
    with open(path, 'w', newline='\n') as f:
        f.write('\n'.join(lines) + '\n')


def _find_mads(project_dir):
    """Search for the MADS assembler binary.

    Checks (in order):
      1. project_dir/mads  (output directory)
      2. Next to the running executable (frozen builds)
      3. PyInstaller bundle (sys._MEIPASS/bin/)
      4. System PATH

    Returns:
        Absolute path to MADS binary, or None if not found.
    """
    import platform as _platform
    name = 'mads.exe' if _platform.system() == 'Windows' else 'mads'

    candidates = [
        os.path.join(project_dir, name),
        os.path.join(os.path.dirname(os.path.abspath(sys.executable)), name),
    ]

    # PyInstaller bundle may include mads in bin/
    if hasattr(sys, '_MEIPASS'):
        candidates.append(os.path.join(sys._MEIPASS, 'bin', name))
        candidates.append(os.path.join(sys._MEIPASS, name))

    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return os.path.abspath(path)

    # Check system PATH
    import shutil as _shutil
    return _shutil.which(name)


def try_assemble(output_dir):
    """Assemble the project, preferring external MADS over built-in.

    Strategy:
      1. Look for MADS in the project folder and system PATH.
      2. If found, invoke MADS.  On success → return ('mads').
      3. If MADS not found or MADS fails, fall back to built-in assembler.

    Args:
        output_dir: Directory containing the generated ASM project.

    Returns:
        (xex_path, method) on success — method is 'mads' or 'built-in'.
        (None, error_msg) on failure.
    """
    import subprocess

    asm_path = os.path.join(output_dir, 'stream_player.asm')
    xex_path = os.path.join(output_dir, 'stream_player.xex')

    # ── Try external MADS first ───────────────────────────────────────
    mads_bin = _find_mads(output_dir)
    if mads_bin:
        try:
            result = subprocess.run(
                [mads_bin, asm_path, f'-o:{xex_path}'],
                capture_output=True, text=True, timeout=30)
            if result.returncode == 0 and os.path.isfile(xex_path):
                return xex_path, 'mads'
            # MADS failed — fall through to built-in
            mads_err = (result.stderr or result.stdout).strip()
            print(f"  MADS failed: {mads_err[:200]}")
            print(f"  Falling back to built-in assembler...")
        except (subprocess.TimeoutExpired, OSError) as e:
            print(f"  MADS error: {e}")
            print(f"  Falling back to built-in assembler...")

    # ── Fall back to built-in assembler ───────────────────────────────
    from .simple_mads import assemble as builtin_assemble
    from .simple_mads.assembler import AsmError

    try:
        xex_data = builtin_assemble(asm_path)
        with open(xex_path, 'wb') as f:
            f.write(xex_data)
        return xex_path, 'built-in'
    except AsmError as e:
        return None, f"Assembly failed: {e}"
