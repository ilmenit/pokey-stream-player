"""Multi-pass 6502 assembler: resolve symbols, emit bytes, build XEX.

Three-phase architecture:
  1. **Parse** (``parser.py``): files → flat ``[Stmt]`` list
  2. **Resolve** (this module): iterate ``[Stmt]`` assigning PCs and
     collecting symbols until the symbol table converges.
  3. **Emit** (this module): single final pass encoding every statement
     into segments → XEX binary.

Unresolved references are detected structurally: any statement that
fails to evaluate after convergence is an error.  No manual tracking.
"""

import os
import struct

from .expressions import evaluate, ExprError
from .encoder import encode, EncodeError
from .xex import Segment, build_xex, make_init_segment
from .parser import parse, Loc, Stmt, ParseError


# ══════════════════════════════════════════════════════════════════════
# ERROR REPORTING
# ══════════════════════════════════════════════════════════════════════

class AsmError(Exception):
    """Rich assembly error with source context.

    Renders as::

        irq_vq.asm:60: Branch out of range (offset +140)
           60 |     beq irq_exit
          included from stream_player.asm:105
          hint: Branch range is ±127 bytes. Use JMP for longer jumps.
    """

    def __init__(self, msg, loc=None, *, hint=''):
        self.msg = msg
        self.loc = loc
        self.hint = hint
        super().__init__(self._format())

    def _format(self):
        parts = []
        if self.loc and self.loc.file:
            parts.append(f"{os.path.basename(self.loc.file)}"
                         f":{self.loc.line}: {self.msg}")
        else:
            parts.append(self.msg)
        if self.loc and self.loc.source:
            parts.append(f"  {self.loc.line:5d} | {self.loc.source}")
        if self.loc:
            for fn, ln in reversed(self.loc.inc_stack):
                parts.append(f"  included from {os.path.basename(fn)}:{ln}")
        if self.hint:
            parts.append(f"  hint: {self.hint}")
        return '\n'.join(parts)


def _hint(msg):
    """Actionable hint derived from error text."""
    m = msg.lower()
    if 'undefined symbol' in m:
        return ("Check spelling — symbols are case-sensitive. "
                "If defined in another file, ensure it is included first.")
    if 'branch out of range' in m:
        return ("Branch range is ±127 bytes. "
                "Use JMP for longer distances, or restructure the code.")
    if 'not found' in m:
        return "Check the filename and that the file is in the project directory."
    return ''


def _loc_error(msg, loc):
    """Build an AsmError from a Loc and message."""
    return AsmError(msg, loc, hint=_hint(msg))


# ══════════════════════════════════════════════════════════════════════
# ASSEMBLER
# ══════════════════════════════════════════════════════════════════════

MAX_PASSES = 20


class Assembler:
    """Three-phase 6502 assembler: parse → resolve → emit."""

    def __init__(self, filename, search_dirs=None):
        self.main_file = os.path.abspath(filename)
        self.base_dir = os.path.dirname(self.main_file)
        self.search_dirs = [self.base_dir] + (search_dirs or [])
        self._cache = {}  # shared file cache

    def assemble(self):
        """Assemble to XEX bytes.

        Returns:
            ``bytes`` — complete Atari XEX binary.

        Raises:
            AsmError with full context on any failure.
        """
        symbols = {}
        prev = None
        history = []

        for pass_n in range(1, MAX_PASSES + 1):
            # Phase 1: parse (re-run each pass for conditional re-eval)
            try:
                stmts = parse(self.main_file, symbols,
                              self._cache, self.search_dirs)
            except ParseError as e:
                raise _loc_error(str(e), e.loc)

            # Phase 2: resolve
            symbols, unresolved = self._resolve(stmts, symbols)
            history.append(dict(symbols))

            if prev == symbols and pass_n >= 2:
                if unresolved:
                    self._raise_unresolved(stmts, unresolved, symbols)
                # Phase 3: emit
                return build_xex(self._emit(stmts, symbols))

            prev = dict(symbols)

        self._raise_no_convergence(history)

    # ── Phase 2: resolve ──────────────────────────────────────────────

    @staticmethod
    def _resolve(stmts, prev_symbols):
        """One resolve pass.  Returns ``(symbols, [(index, pc), ...])``."""
        syms = dict(prev_symbols)
        pc = 0
        bad = []   # [(stmt_index, pc_at_stmt), ...]

        for i, s in enumerate(stmts):
            k = s.kind

            if k == 'label':
                syms[s.name] = pc & 0xFFFF

            elif k == 'equate':
                try:
                    syms[s.name] = evaluate(s.expr, syms, pc) & 0xFFFF
                except ExprError:
                    bad.append((i, pc))

            elif k == 'org':
                try:
                    pc = evaluate(s.expr, syms, pc) & 0xFFFF
                except ExprError:
                    bad.append((i, pc))

            elif k == 'ini':
                try:
                    evaluate(s.expr, syms, pc)
                except ExprError:
                    bad.append((i, pc))

            elif k == 'byte':
                any_bad = False
                for expr in s.exprs:
                    try:    evaluate(expr, syms, pc)
                    except ExprError:
                        if not any_bad:
                            bad.append((i, pc))
                            any_bad = True
                    pc += 1

            elif k == 'word':
                any_bad = False
                for expr in s.exprs:
                    try:    evaluate(expr, syms, pc)
                    except ExprError:
                        if not any_bad:
                            bad.append((i, pc))
                            any_bad = True
                    pc += 2

            elif k == 'instr':
                try:
                    pc += len(encode(s.name, s.expr, syms, pc))
                except (ExprError, EncodeError):
                    bad.append((i, pc))
                    pc += s.est_size

            # 'error' stmts: no PC change, handled in emit

        return syms, bad

    # ── Phase 3: emit ─────────────────────────────────────────────────

    @staticmethod
    def _emit(stmts, symbols):
        """Final emit pass.  Returns ``[Segment, ...]``.

        Every expression must resolve; failures raise AsmError.
        """
        segs = []
        cur = None   # current Segment or None
        pc = 0

        def close():
            nonlocal cur
            if cur and cur.data:
                segs.append(cur)
            cur = None

        def put(data):
            nonlocal cur, pc
            if cur is None:
                cur = Segment(pc)
            cur.data.extend(data)
            pc += len(data)

        for s in stmts:
            k = s.kind
            try:
                if k in ('label', 'equate'):
                    continue

                elif k == 'org':
                    close()
                    pc = evaluate(s.expr, symbols, pc) & 0xFFFF
                    cur = Segment(pc)

                elif k == 'ini':
                    close()
                    segs.append(make_init_segment(
                        evaluate(s.expr, symbols, pc) & 0xFFFF))

                elif k == 'byte':
                    for expr in s.exprs:
                        put(bytes([evaluate(expr, symbols, pc) & 0xFF]))

                elif k == 'word':
                    for expr in s.exprs:
                        put(struct.pack('<H',
                            evaluate(expr, symbols, pc) & 0xFFFF))

                elif k == 'instr':
                    put(encode(s.name, s.expr, symbols, pc))

                elif k == 'error':
                    raise _loc_error(f".error: {s.expr}", s.loc)

            except (ExprError, EncodeError) as e:
                raise _loc_error(str(e), s.loc)

        close()
        return segs

    # ── Error reporting ───────────────────────────────────────────────

    def _raise_unresolved(self, stmts, bad, symbols):
        """Report first unresolved reference, with count of others."""
        idx, pc = bad[0]
        first = stmts[idx]
        n = len(bad)

        # Re-evaluate with converged symbols + correct PC for precise message
        msg = f"Unresolved reference in: {first.loc.source.strip()}"
        try:
            if first.kind == 'instr':
                encode(first.name, first.expr, symbols, pc)
            elif first.kind in ('equate', 'org', 'ini'):
                evaluate(first.expr, symbols, pc)
            elif first.kind in ('byte', 'word'):
                for expr in first.exprs:
                    evaluate(expr, symbols, pc)
        except (ExprError, EncodeError) as e:
            msg = str(e)

        if n > 1:
            msg += f" (+{n - 1} more)"

        raise _loc_error(msg, first.loc)

    @staticmethod
    def _raise_no_convergence(history):
        """Diagnostic error when symbols keep oscillating."""
        parts = [f"Assembly did not converge after {len(history)} passes."]
        if len(history) >= 4:
            changing = []
            for sym in sorted(history[-1]):
                if sym.startswith('__'):
                    continue
                trail = [h.get(sym) for h in history[-4:]]
                if len(set(v for v in trail if v is not None)) > 1:
                    vals = ' → '.join(
                        f'${v:04X}' if v is not None else '?' for v in trail)
                    changing.append(f"    {sym}: {vals}")
            if changing:
                parts.append("  Symbols that did not stabilize:")
                parts.extend(changing[:10])
                if len(changing) > 10:
                    parts.append(f"    ...and {len(changing) - 10} more")
        raise AsmError('\n'.join(parts))
