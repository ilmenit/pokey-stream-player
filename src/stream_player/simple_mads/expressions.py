"""Expression tokenizer and recursive-descent evaluator.

Handles:
  - Decimal, hex ($FF), binary (%1010) literals
  - Symbol references, * (current PC)
  - Arithmetic: + - * /  (with parentheses)
  - Unary < (lo byte) and > (hi byte) in non-condition context
  - Comparisons: = <> < > <= >= in .if condition context
"""


class ExprError(Exception):
    """Raised when an expression can't be evaluated (e.g. forward ref)."""
    pass


# ── Tokenizer ────────────────────────────────────────────────────────

def _tokenize(text, is_condition):
    """Tokenize expression into (type, value) list.

    Token types: 'num', 'sym', 'pc', 'op', 'unary', 'lparen', 'rparen'
    """
    tokens = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]

        if c in ' \t':
            i += 1
            continue

        # Hex: $FF
        if c == '$':
            j = i + 1
            while j < n and text[j] in '0123456789abcdefABCDEF':
                j += 1
            if j == i + 1:
                raise ExprError(f"Empty hex literal at '{text[i:]}'")
            tokens.append(('num', int(text[i+1:j], 16)))
            i = j
            continue

        # Binary: %1010
        if c == '%':
            j = i + 1
            while j < n and text[j] in '01':
                j += 1
            tokens.append(('num', int(text[i+1:j], 2) if j > i+1 else 0))
            i = j
            continue

        # Decimal number
        if c.isdigit():
            j = i
            while j < n and text[j].isdigit():
                j += 1
            tokens.append(('num', int(text[i:j])))
            i = j
            continue

        # Symbol (identifier)
        if c.isalpha() or c == '_':
            j = i
            while j < n and (text[j].isalnum() or text[j] == '_'):
                j += 1
            tokens.append(('sym', text[i:j]))
            i = j
            continue

        # * = PC reference or multiply
        if c == '*':
            if not tokens or tokens[-1][0] in ('op', 'unary', 'lparen'):
                tokens.append(('pc', 0))
            else:
                tokens.append(('op', '*'))
            i += 1
            continue

        # Two-char comparison operators (condition mode only)
        if is_condition and i + 1 < n:
            pair = text[i:i+2]
            if pair in ('<=', '>=', '<>'):
                tokens.append(('op', pair))
                i += 2
                continue

        # < > — unary lo/hi (normal) or comparison (condition)
        if c in '<>':
            if is_condition:
                tokens.append(('op', c))
            else:
                tokens.append(('unary', c))
            i += 1
            continue

        # = in condition mode
        if c == '=' and is_condition:
            tokens.append(('op', '='))
            i += 1
            continue

        if c in '+-/':
            tokens.append(('op', c))
            i += 1
            continue

        if c == '(':
            tokens.append(('lparen', '('))
            i += 1
            continue
        if c == ')':
            tokens.append(('rparen', ')'))
            i += 1
            continue

        raise ExprError(f"Unexpected character '{c}' in expression '{text}'")

    return tokens


# ── Recursive descent parser ─────────────────────────────────────────

def _precedence(op, is_cond):
    """Operator precedence (higher = binds tighter)."""
    if is_cond and op in ('=', '<>'):
        return 0
    if is_cond and op in ('<', '>', '<=', '>='):
        return 1
    if op in ('+', '-'):
        return 2
    if op in ('*', '/'):
        return 3
    return -1


def _parse_atom(tokens, symbols, pc, is_cond):
    """Parse an atomic value: number, symbol, pc, unary, or parenthesized."""
    if not tokens:
        raise ExprError("Unexpected end of expression")

    typ, val = tokens[0]

    # Unary < (lo byte) or > (hi byte)
    if typ == 'unary':
        tokens.pop(0)
        operand = _parse_atom(tokens, symbols, pc, is_cond)
        return (operand & 0xFF) if val == '<' else ((operand >> 8) & 0xFF)

    # Unary minus
    if typ == 'op' and val == '-':
        tokens.pop(0)
        return -_parse_atom(tokens, symbols, pc, is_cond)

    # Parenthesized subexpression
    if typ == 'lparen':
        tokens.pop(0)
        result = _parse_binary(tokens, symbols, pc, is_cond, 0)
        if tokens and tokens[0][0] == 'rparen':
            tokens.pop(0)
        else:
            raise ExprError("Missing closing parenthesis")
        return result

    # Number literal
    if typ == 'num':
        tokens.pop(0)
        return val

    # Current PC
    if typ == 'pc':
        tokens.pop(0)
        return pc

    # Symbol reference
    if typ == 'sym':
        tokens.pop(0)
        if val in symbols:
            return symbols[val]
        raise ExprError(f"Undefined symbol '{val}'")

    raise ExprError(f"Unexpected token: {typ}={val}")


def _parse_binary(tokens, symbols, pc, is_cond, min_prec):
    """Parse binary expression with precedence climbing."""
    left = _parse_atom(tokens, symbols, pc, is_cond)

    while tokens and tokens[0][0] == 'op':
        op = tokens[0][1]
        prec = _precedence(op, is_cond)
        if prec < min_prec:
            break
        tokens.pop(0)
        right = _parse_binary(tokens, symbols, pc, is_cond, prec + 1)

        if   op == '+':  left = left + right
        elif op == '-':  left = left - right
        elif op == '*':  left = left * right
        elif op == '/':  left = left // right if right else 0
        elif op == '=' and is_cond:  left = int(left == right)
        elif op == '<>' and is_cond: left = int(left != right)
        elif op == '<' and is_cond:  left = int(left < right)
        elif op == '>' and is_cond:  left = int(left > right)
        elif op == '<=' and is_cond: left = int(left <= right)
        elif op == '>=' and is_cond: left = int(left >= right)
        else:
            raise ExprError(f"Unknown operator '{op}'")

    return left


# ── Public API ────────────────────────────────────────────────────────

def evaluate(text, symbols, pc, is_condition=False):
    """Evaluate an expression string to an integer.

    Args:
        text: Expression (e.g. "BANK_BASE + (256 * VEC_SIZE)")
        symbols: {name: int} symbol table
        pc: Current program counter
        is_condition: If True, = < > etc. are comparison ops.

    Returns:
        Integer value (masked to 32-bit unsigned).

    Raises:
        ExprError on undefined symbols or syntax errors.
    """
    tokens = _tokenize(text.strip(), is_condition)
    if not tokens:
        raise ExprError("Empty expression")
    val = _parse_binary(tokens, symbols, pc, is_condition, 0)
    return val & 0xFFFFFFFF
