"""Phase 1: Parse source files into a flat statement list.

Reads all files (cached), expands ``icl`` includes, evaluates
``.if``/``.elseif``/``.else``/``.endif`` conditionals, rewrites
``@local`` labels to file-scoped internal names, and produces a flat
list of :class:`Stmt` objects ready for the resolve/emit phases.
"""

import os
import re

from .expressions import evaluate, ExprError
from .encoder import estimate_size
from .opcodes import ALL_MNEMONICS


# ══════════════════════════════════════════════════════════════════════
# DATA TYPES
# ══════════════════════════════════════════════════════════════════════

class Loc:
    """Immutable source location for error reporting."""
    __slots__ = ('file', 'line', 'source', 'inc_stack')

    def __init__(self, file, line, source='', inc_stack=()):
        self.file = file
        self.line = line
        self.source = source
        self.inc_stack = inc_stack


class Stmt:
    """One parsed statement.

    Fields are overloaded by *kind*:

    ========  ============================  ======================
    kind      name                          expr / exprs
    ========  ============================  ======================
    label     symbol name                   —
    equate    symbol name                   value expression
    org       —                             address expression
    ini       —                             address expression
    byte      —                             exprs = (expr, …)
    word      —                             exprs = (expr, …)
    instr     mnemonic                      operand string
    error     —                             error message text
    ========  ============================  ======================
    """
    __slots__ = ('kind', 'loc', 'name', 'expr', 'exprs', 'est_size')

    def __init__(self, kind, loc, name='', expr='', exprs=(), est_size=0):
        self.kind = kind
        self.loc = loc
        self.name = name
        self.expr = expr
        self.exprs = exprs
        self.est_size = est_size


class ParseError(Exception):
    """Hard error during parsing (missing include, conditional mismatch)."""
    def __init__(self, msg, loc):
        self.loc = loc
        super().__init__(msg)


# ══════════════════════════════════════════════════════════════════════
# TEXT HELPERS
# ══════════════════════════════════════════════════════════════════════

def _strip_comment(line):
    """Remove ``; comment``, respecting quoted strings."""
    in_q, qc = False, None
    for i, c in enumerate(line):
        if in_q:
            if c == qc: in_q = False
        elif c in ('"', "'"):
            in_q, qc = True, c
        elif c == ';':
            return line[:i].rstrip()
    return line.rstrip()


def _dir_match(low, directive):
    """Does lowercased line start with *directive* + whitespace/EOL?"""
    n = len(directive)
    return low.startswith(directive) and (len(low) == n or low[n] in ' \t')


def _dir_after(text, directive):
    """Return text after *directive* keyword, with leading whitespace stripped."""
    i = len(directive)
    while i < len(text) and text[i] in ' \t':
        i += 1
    return text[i:]


def split_data_args(s):
    """Split comma-separated args respecting parentheses."""
    parts, depth, cur = [], 0, []
    for c in s:
        if c == '(':   depth += 1
        elif c == ')': depth -= 1
        elif c == ',' and depth == 0:
            parts.append(''.join(cur).strip())
            cur = []
            continue
        cur.append(c)
    tail = ''.join(cur).strip()
    if tail:
        parts.append(tail)
    return tuple(parts)


# ── Regex patterns ────────────────────────────────────────────────────

_RE_GLOBAL = re.compile(r'^([A-Za-z_]\w*)\s*:')
_RE_LOCAL  = re.compile(r'^@([A-Za-z_]\w*)\s*:')
_RE_ANON   = re.compile(r'^@\s*:')
_RE_EQUATE = re.compile(r'^([A-Za-z_]\w*)\s*=\s*(.+)$')
_RE_ICL    = re.compile(r"^\s*icl\s+['\"]([^'\"]+)['\"]", re.I)
_RE_ATREF  = re.compile(r'@([A-Za-z_]\w*)')


# ══════════════════════════════════════════════════════════════════════
# PARSER
# ══════════════════════════════════════════════════════════════════════

def parse(main_file, symbols, file_cache, search_dirs):
    """Parse all source files into a flat statement list.

    Args:
        main_file:   Absolute path to the main ``.asm`` file.
        symbols:     Symbol dict for conditional evaluation.
        file_cache:  Shared ``{path: [lines]}`` dict (populated on read).
        search_dirs: Extra directories to search for ``icl`` files.

    Returns:
        ``list[Stmt]`` — flat, ordered list ready for resolve/emit.

    Raises:
        ParseError on missing includes or conditional mismatches.
    """
    p = _Parser(symbols, file_cache, search_dirs)
    p.process_file(main_file)
    if p.cond_stack:
        raise ParseError(
            f"Unclosed .if ({len(p.cond_stack)} level(s) deep)",
            Loc(main_file, 0))
    return p.out


class _Parser:
    """Internal parse state."""

    def __init__(self, symbols, file_cache, search_dirs):
        self.symbols = symbols
        self.cache = file_cache
        self.dirs = search_dirs
        self.out = []               # [Stmt, ...]
        self.cond_stack = []        # [(kind, active, any_true)]
        self.inc_stack = []         # [(file, line)]
        self.file_ids = {}          # {abspath: int}
        self.anon_n = 0             # next anonymous label number
        self.cur_file = ''

    # ── Properties ────────────────────────────────────────────────────

    @property
    def active(self):
        return not self.cond_stack or self.cond_stack[-1][1]

    def _parent_active(self):
        return len(self.cond_stack) <= 1 or self.cond_stack[-2][1]

    # ── Helpers ───────────────────────────────────────────────────────

    def _fid(self):
        f = self.cur_file
        if f not in self.file_ids:
            self.file_ids[f] = len(self.file_ids)
        return self.file_ids[f]

    def _lk(self, name):
        """File-scoped internal key: ``__f3_copy_loop``."""
        return f'__f{self._fid()}_{name}'

    def _resolve(self, expr):
        """Rewrite ``@name`` → internal key, ``@+`` → next anon label."""
        if '@' not in expr:
            return expr
        if '@+' in expr:
            return expr.replace('@+', f'__anon_{self.anon_n}')
        return _RE_ATREF.sub(lambda m: self._lk(m.group(1)), expr)

    def _loc(self, fn, ln):
        raw = self.cache.get(fn, [])
        src = raw[ln - 1].rstrip() if 0 < ln <= len(raw) else ''
        return Loc(fn, ln, src, tuple(self.inc_stack))

    def _read(self, path):
        if path not in self.cache:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                self.cache[path] = f.readlines()
        return self.cache[path]

    def _find(self, name, referrer, loc):
        for d in [os.path.dirname(referrer)] + self.dirs:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return os.path.abspath(p)
        raise ParseError(f"Include file not found: '{name}'", loc)

    # ── File + line processing ────────────────────────────────────────

    def process_file(self, filename):
        prev = self.cur_file
        self.cur_file = filename
        for ln, raw in enumerate(self._read(filename), 1):
            text = _strip_comment(raw).strip()
            if text:
                self._line(text, filename, ln)
        self.cur_file = prev

    def _line(self, text, fn, ln):
        low = text.lower()
        loc = self._loc(fn, ln)

        # ── Conditionals (always processed, even if inactive) ─────────
        if _dir_match(low, '.if'):
            if self.active:
                try: v = evaluate(_dir_after(text,'.if'), self.symbols, 0, True)
                except ExprError: v = 0
                self.cond_stack.append(('if', bool(v), bool(v)))
            else:
                self.cond_stack.append(('if', False, True))
            return
        if _dir_match(low, '.elseif'):
            if not self.cond_stack:
                raise ParseError(".elseif without .if", loc)
            _, _, hit = self.cond_stack[-1]
            if not self._parent_active() or hit:
                self.cond_stack[-1] = ('ei', False, hit)
            else:
                try: v = evaluate(_dir_after(text,'.elseif'), self.symbols, 0, True)
                except ExprError: v = 0
                self.cond_stack[-1] = ('ei', bool(v), bool(v))
            return
        if low == '.else':
            if not self.cond_stack:
                raise ParseError(".else without .if", loc)
            _, _, hit = self.cond_stack[-1]
            self.cond_stack[-1] = ('el', self._parent_active() and not hit, True)
            return
        if low == '.endif':
            if not self.cond_stack:
                raise ParseError(".endif without .if", loc)
            self.cond_stack.pop()
            return
        if not self.active:
            return

        # ── Include ───────────────────────────────────────────────────
        m = _RE_ICL.match(text)
        if m:
            path = self._find(m.group(1), fn, loc)
            self.inc_stack.append((fn, ln))
            self.process_file(path)
            self.inc_stack.pop()
            return

        # ── Anonymous label @: ────────────────────────────────────────
        if _RE_ANON.match(text):
            self.out.append(Stmt('label', loc, name=f'__anon_{self.anon_n}'))
            self.anon_n += 1
            rest = text[text.index(':') + 1:].strip()
            if rest: self._line(rest, fn, ln)
            return

        # ── @local label ──────────────────────────────────────────────
        m = _RE_LOCAL.match(text)
        if m:
            self.out.append(Stmt('label', loc, name=self._lk(m.group(1))))
            rest = text[m.end():].strip()
            if rest: self._line(rest, fn, ln)
            return

        # ── Global label ──────────────────────────────────────────────
        m = _RE_GLOBAL.match(text)
        if m:
            self.out.append(Stmt('label', loc, name=m.group(1)))
            rest = text[m.end():].strip()
            if rest: self._line(rest, fn, ln)
            return

        # ── Equate ────────────────────────────────────────────────────
        m = _RE_EQUATE.match(text)
        if m and m.group(1).lower() not in ALL_MNEMONICS:
            self.out.append(Stmt('equate', loc, name=m.group(1),
                                 expr=self._resolve(m.group(2).strip())))
            return

        # ── Directives ────────────────────────────────────────────────
        for tag in ('org', 'ini'):
            if _dir_match(low, tag):
                self.out.append(Stmt(tag, loc,
                                     expr=self._resolve(_dir_after(text, tag))))
                return
        for tag, w in (('.byte', 1), ('.word', 2)):
            if _dir_match(low, tag):
                args = split_data_args(_dir_after(text, tag))
                args = tuple(self._resolve(a) for a in args)
                self.out.append(Stmt(tag[1:], loc, exprs=args,
                                     est_size=w * len(args)))
                return
        if _dir_match(low, '.error'):
            msg = _dir_after(text, '.error').strip('"').strip("'")
            self.out.append(Stmt('error', loc, expr=msg))
            return

        # ── Instruction ───────────────────────────────────────────────
        parts = text.split(None, 1)
        mn = parts[0].lower()
        op = self._resolve(parts[1].strip()) if len(parts) > 1 else ''
        if mn not in ALL_MNEMONICS:
            self.out.append(Stmt('error', loc,
                                 expr=f"Unknown instruction: '{mn}'"))
            return
        self.out.append(Stmt('instr', loc, name=mn, expr=op,
                             est_size=estimate_size(mn, op)))
