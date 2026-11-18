"""Tokenised BASIC programs <-> readable text.

  Commodore BASIC V2 / 3.5 / 7.0 (C64, VIC-20, PET, C16, Plus/4, C128)   list + tokenise
  Sinclair ZX Spectrum BASIC (48K / 128K)                                list + tokenise
  BBC BASIC (BBC Micro, Electron, Master)                                list + tokenise

Listings use {..} for characters that can't be typed, e.g. {clr}, {rvs on}, {$9b}, {UDG A}; the tokenisers
accept the same codes, so a listing can be edited and turned back into a program.
"""
from __future__ import annotations

import math
import re
import struct

# ============================================================================ Commodore
CBM_TOKENS = (
    "END FOR NEXT DATA INPUT# INPUT DIM READ LET GOTO RUN IF RESTORE GOSUB RETURN REM STOP ON WAIT LOAD SAVE "
    "VERIFY DEF POKE PRINT# PRINT CONT LIST CLR CMD SYS OPEN CLOSE GET NEW TAB( TO FN SPC( THEN NOT STEP + - * / ^ "
    "AND OR > = < SGN INT ABS USR FRE POS SQR RND LOG EXP COS SIN TAN ATN PEEK LEN STR$ VAL ASC CHR$ LEFT$ RIGHT$ "
    "MID$ GO "
    # BASIC 3.5 (C16 / Plus/4) and 7.0 (C128) single-byte tokens
    "RGR RCLR RLUM JOY RDOT DEC HEX$ ERR$ INSTR ELSE RESUME TRAP TRON TROFF SOUND VOL AUTO PUDEF GRAPHIC PAINT "
    "CHAR BOX CIRCLE GSHAPE SSHAPE DRAW LOCATE COLOR SCNCLR SCALE HELP DO LOOP EXIT DIRECTORY DSAVE DLOAD HEADER "
    "SCRATCH COLLECT COPY RENAME BACKUP DELETE RENUMBER KEY MONITOR USING UNTIL WHILE").split()
CBM_V2_COUNT = 76                         # 0x80-0xCB: what a C64 / VIC-20 understands
CBM_TOKEN = {i + 0x80: t for i, t in enumerate(CBM_TOKENS)}
CBM_TOKEN[0xFF] = "{pi}"

CBM_CODES = {
    0x05: "wht", 0x07: "bell", 0x08: "lock", 0x09: "unlock", 0x0D: "return", 0x0E: "lower", 0x11: "down",
    0x12: "rvs on", 0x13: "home", 0x14: "del", 0x1C: "red", 0x1D: "right", 0x1E: "grn", 0x1F: "blu",
    0x81: "orng", 0x85: "f1", 0x86: "f3", 0x87: "f5", 0x88: "f7", 0x89: "f2", 0x8A: "f4", 0x8B: "f6", 0x8C: "f8",
    0x8D: "shift return", 0x8E: "upper", 0x90: "blk", 0x91: "up", 0x92: "rvs off", 0x93: "clr", 0x94: "inst",
    0x95: "brn", 0x96: "lred", 0x97: "gry1", 0x98: "gry2", 0x99: "lgrn", 0x9A: "lblu", 0x9B: "gry3",
    0x9C: "pur", 0x9D: "left", 0x9E: "yel", 0x9F: "cyn", 0xFF: "pi", 0xA0: "shift space",
}
CBM_CODE_BY_NAME = {v: k for k, v in CBM_CODES.items()}
CBM_CODE_BY_NAME.update({"white": 0x05, "clear": 0x93, "reverse on": 0x12, "reverse off": 0x92, "rvon": 0x12,
                         "rvof": 0x92, "purple": 0x9C, "yellow": 0x9E, "cyan": 0x9F, "black": 0x90, "green": 0x1E,
                         "blue": 0x1F, "orange": 0x81, "brown": 0x95, "space": 0x20, "crsr down": 0x11,
                         "crsr up": 0x91, "crsr right": 0x1D, "crsr left": 0x9D, "π": 0xFF})

# where BASIC programs load, by machine (the 2-byte address at the start of a .PRG)
CBM_BASIC_STARTS = {0x0401: "PET / VIC-20 +3K", 0x0801: "C64", 0x1001: "VIC-20 / C16 / Plus/4",
                    0x1201: "VIC-20 +8K", 0x1C01: "C128", 0x4001: "C128 (graphics on)"}


def petscii_char(c: int, quoted: bool) -> str:
    if 0x20 <= c <= 0x40 or c in (0x5B, 0x5D):
        return chr(c)
    if 0x41 <= c <= 0x5A:
        return chr(c)
    if 0xC1 <= c <= 0xDA or 0x61 <= c <= 0x7A:     # shifted letters (graphics in upper-case mode)
        return chr((c & 0x7F) | 0x20) if c >= 0xC1 else chr(c)
    if c == 0x5C:
        return "£"
    if c == 0x5E:
        return "↑"
    if c == 0x5F:
        return "←"
    if c in CBM_CODES:
        return "{" + CBM_CODES[c] + "}"
    return "{$%02x}" % c


def cbm_is_basic(prg: bytes) -> bool:
    return cbm_parse(prg) is not None


def cbm_parse(prg: bytes):
    """-> (load address, [(line number, raw line bytes)], bytes after the program) or None if not BASIC."""
    if len(prg) < 6:
        return None
    load = prg[0] | prg[1] << 8
    if load not in CBM_BASIC_STARTS:
        return None
    pos, lines, last = 2, [], -1
    while pos + 2 <= len(prg):
        link = prg[pos] | prg[pos + 1] << 8
        if link == 0:
            pos += 2
            break
        if link <= load + pos - 2 or link > load + len(prg) + 0x100:     # links must point forwards
            return None
        if pos + 4 > len(prg):
            return None
        num = prg[pos + 2] | prg[pos + 3] << 8
        end = prg.find(b"\x00", pos + 4)
        if end < 0 or num > 63999 or num < last or end - pos > 255:
            return None
        lines.append((num, prg[pos + 4:end]))
        last, pos = num, end + 1
        if len(lines) > 20000:
            return None
    else:
        if not lines:
            return None
    if not lines:
        return None
    return load, lines, prg[pos:]


def cbm_list(prg: bytes) -> str | None:
    """Listing of a Commodore BASIC .PRG (with its 2-byte load address), or None if it isn't BASIC."""
    p = cbm_parse(prg)
    if p is None:
        return None
    load, lines, rest = p
    out = []
    for num, raw in lines:
        s, quoted, literal, i = [], False, False, 0
        while i < len(raw):
            c = raw[i]
            if c == 0x22:
                quoted = not quoted
                s.append('"')
            elif quoted or literal or c < 0x80:
                s.append(petscii_char(c, quoted or literal))
            elif c in (0xCE, 0xFE) and load in (0x1C01, 0x4001) and i + 1 < len(raw):   # C128 2-byte tokens
                s.append("{$%02x}{$%02x}" % (c, raw[i + 1]))
                i += 1
            else:
                tok = CBM_TOKEN.get(c)
                s.append(tok if tok else "{$%02x}" % c)
                if c == 0x8F:                    # REM: rest of the line is literal
                    literal = True
            i += 1
        out.append(f"{num} {''.join(s)}")
    text = "\n".join(out) + "\n"
    if rest.strip(b"\x00"):
        text += f"\n(+ {len(rest)} bytes of machine code after the BASIC program)\n"
    return text


_CODE_RE = re.compile(r"\{([^{}]*)\}")


def _cbm_brace(name: str) -> list[int]:
    """{code} -> bytes; supports {clr}, {$93}, {3 down} / {down*3}, {shift-a}."""
    n = name.strip().lower()
    rep = 1
    m = re.match(r"^(\d+)\s*(.+)$", n) or None
    if m:
        rep, n = int(m.group(1)), m.group(2).strip()
    m = re.match(r"^(.+?)\s*\*\s*(\d+)$", n)
    if m:
        n, rep = m.group(1).strip(), int(m.group(2))
    if n.startswith("$") and re.fullmatch(r"\$[0-9a-f]{1,2}", n):
        return [int(n[1:], 16)] * rep
    if re.fullmatch(r"(shift|s)[- ]([a-z])", n):
        return [0xC1 + ord(n[-1]) - ord("a")] * rep
    if n in CBM_CODE_BY_NAME:
        return [CBM_CODE_BY_NAME[n]] * rep
    if n == "pi":
        return [0xFF] * rep
    raise ValueError(f"Unknown code {{{name}}}")


def _cbm_text_char(ch: str) -> int:
    o = ord(ch)
    if ch == "£":
        return 0x5C
    if ch == "↑":
        return 0x5E
    if ch == "←":
        return 0x5F
    if ch == "π":
        return 0xFF
    if "a" <= ch <= "z":
        return 0xC1 + o - 0x61
    if 0x20 <= o <= 0x5F:
        return o
    raise ValueError(f"Character {ch!r} has no PETSCII equivalent - use a {{$xx}} code")


def cbm_tokenise(source: str, load: int = 0x0801, v2_only: bool = True) -> bytes:
    """BASIC text -> .PRG bytes (with the load address). Keywords are matched the way the C64 does it; text in
    quotes keeps its case: CAPITALS are normal letters, lower case becomes shifted characters. A program typed
    entirely in lower case is treated as capitals."""
    lines = [ln.rstrip("\r") for ln in source.replace("\r\n", "\n").split("\n")]
    if not re.search(r"[A-Z]", re.sub(r"\{[^{}]*\}", "", source)):
        lines = [_upper_outside_codes(ln) for ln in lines]
    tokens = CBM_TOKENS[:CBM_V2_COUNT] if v2_only else CBM_TOKENS
    out = bytearray(struct.pack("<H", load))
    addr, last = load, -1
    for raw in lines:
        if not raw.strip():
            continue
        m = re.match(r"\s*(\d+)\s?(.*)$", raw)
        if not m:
            raise ValueError(f"Line without a line number: {raw!r}")
        num, text = int(m.group(1)), m.group(2)
        if num > 63999:
            raise ValueError(f"Line number {num} is too large (max 63999)")
        if num <= last:
            raise ValueError(f"Line {num} is out of order (after {last})")
        last = num
        body = bytearray()
        i, quoted, literal, data = 0, False, False, False
        while i < len(text):
            ch = text[i]
            if ch == "{":
                j = text.find("}", i)
                if j < 0:
                    raise ValueError(f"Line {num}: unclosed {{")
                body += bytes(_cbm_brace(text[i + 1:j]))
                i = j + 1
                continue
            if ch == '"':
                quoted = not quoted
                body.append(0x22)
                i += 1
                continue
            if quoted or literal:
                body.append(_cbm_text_char(ch))
                i += 1
                continue
            if data:
                if ch == ":":
                    data = False
                body.append(_cbm_text_char(ch.upper()))
                i += 1
                continue
            up = text[i:].upper()
            for k, t in enumerate(tokens):
                if up.startswith(t):
                    body.append(0x80 + k)
                    i += len(t)
                    if t == "REM":
                        literal = True
                    elif t == "DATA":
                        data = True
                    break
            else:
                body.append(_cbm_text_char(ch.upper()))
                i += 1
        if len(body) > 250:
            raise ValueError(f"Line {num} is too long once tokenised ({len(body)} bytes, max 250)")
        addr += 4 + len(body) + 1
        out += struct.pack("<HH", addr, num) + body + b"\x00"
    if last < 0:
        raise ValueError("No BASIC lines found")
    out += b"\x00\x00"
    return bytes(out)


def _upper_outside_codes(s: str) -> str:
    return re.sub(r"(\{[^{}]*\})|([^{]+)", lambda m: m.group(1) or m.group(2).upper(), s)


# ============================================================================ ZX Spectrum
ZX_TOKENS = (
    "SPECTRUM", "PLAY", "RND", "INKEY$", "PI", "FN", "POINT", "SCREEN$", "ATTR", "AT", "TAB", "VAL$", "CODE", "VAL",
    "LEN", "SIN", "COS", "TAN", "ASN", "ACS", "ATN", "LN", "EXP", "INT", "SQR", "SGN", "ABS", "PEEK", "IN", "USR",
    "STR$", "CHR$", "NOT", "BIN", "OR", "AND", "<=", ">=", "<>", "LINE", "THEN", "TO", "STEP", "DEF FN", "CAT",
    "FORMAT", "MOVE", "ERASE", "OPEN #", "CLOSE #", "MERGE", "VERIFY", "BEEP", "CIRCLE", "INK", "PAPER", "FLASH",
    "BRIGHT", "INVERSE", "OVER", "OUT", "LPRINT", "LLIST", "STOP", "READ", "DATA", "RESTORE", "NEW", "BORDER",
    "CONTINUE", "DIM", "REM", "FOR", "GO TO", "GO SUB", "INPUT", "LOAD", "LIST", "LET", "PAUSE", "NEXT", "POKE",
    "PRINT", "PLOT", "RUN", "SAVE", "RANDOMIZE", "IF", "CLS", "DRAW", "CLEAR", "RETURN", "COPY")
ZX_TOKEN = {0xA3 + i: t for i, t in enumerate(ZX_TOKENS)}
ZX_FUNCTIONS = set(ZX_TOKENS[2:39])          # RND .. <> : functions/operators (no space before in listings)
ZX_CTRL_PARAMS = {0x10: 1, 0x11: 1, 0x12: 1, 0x13: 1, 0x14: 1, 0x15: 1, 0x16: 2, 0x17: 2}   # INK..TAB codes


def zx_char(c: int) -> str:
    if c == 0x60:
        return "£"
    if c == 0x7F:
        return "©"
    if c == 0x5E:
        return "↑"
    if 0x20 <= c < 0x7F:
        return chr(c)
    if 0x80 <= c <= 0x8F:
        return "{block %d}" % (c - 0x80)
    if 0x90 <= c <= 0xA2:
        return "{UDG %s}" % chr(ord("A") + c - 0x90)
    return "{$%02x}" % c


def zx_list(prog: bytes, autostart: int | None = None) -> str | None:
    """Listing of a Spectrum BASIC program (the data block of a 'Program:' file)."""
    pos, out = 0, []
    while pos + 4 <= len(prog):
        num = prog[pos] << 8 | prog[pos + 1]
        if num > 16383:                  # end of program / start of variables
            break
        ln = prog[pos + 2] | prog[pos + 3] << 8
        body = prog[pos + 4:pos + 4 + ln]
        if len(body) < ln or not body or body[-1] != 0x0D:
            if out:
                break
            return None
        out.append(f"{num} " + _zx_line(body[:-1]).strip())
        pos += 4 + ln
    if not out:
        return None
    return "\n".join(out) + "\n"


def _zx_line(b: bytes) -> str:
    out: list[str] = []
    last = lambda: out[-1][-1] if out and out[-1] else ""          # noqa: E731
    i, quoted, rem, space_after = 0, False, False, False
    while i < len(b):
        c = b[i]
        if not quoted and not rem and c == 0x0E and i + 5 < len(b):   # hidden 5-byte form of a number
            i += 6
            continue
        if space_after:
            space_after = False
            if c != 0x20 and chr(c) not in ":;,)=+-*/<>^" and c < 0xA3:
                out.append(" ")
        if c == 0x22 and not rem:
            quoted = not quoted
            out.append('"')
        elif c >= 0xA3 and not quoted and not rem:
            tok = ZX_TOKEN[c]
            if tok in ("<=", ">=", "<>"):
                out.append(tok)
            else:
                if out and (last().isalnum() or last() in '"$)'):
                    out.append(" ")
                out.append(tok)
                space_after = True
            if c == 0xEA:
                rem = True
        elif c in ZX_CTRL_PARAMS and i + ZX_CTRL_PARAMS[c] < len(b):
            n = ZX_CTRL_PARAMS[c]
            out.append("".join("{$%02x}" % x for x in b[i:i + 1 + n]))
            i += n
        else:
            out.append(zx_char(c))
        i += 1
    return "".join(out)


def zx_float(x: float) -> bytes:
    """A number in the Spectrum's 5-byte form (small integers use the short integer form)."""
    if float(x).is_integer() and -65535 <= x <= 65535:
        v = int(x)
        sign = 0xFF if v < 0 else 0x00
        v = v & 0xFFFF if v >= 0 else (v + 65536) & 0xFFFF
        return bytes([0, sign, v & 0xFF, v >> 8, 0])
    if x == 0:
        return bytes(5)
    neg = x < 0
    x = abs(x)
    e = math.floor(math.log2(x)) + 1
    m = x / 2 ** e
    if m >= 1:
        m, e = m / 2, e + 1
    if m < 0.5:
        m, e = m * 2, e - 1
    mant = int(round(m * 2 ** 32))
    if mant >= 2 ** 32:
        mant, e = mant >> 1, e + 1
    if not -127 <= e <= 127:
        raise ValueError(f"Number {x} out of range")
    mant = (mant & 0x7FFFFFFF) | (0x80000000 if neg else 0)
    return bytes([e + 128]) + struct.pack(">I", mant)


_ZX_SORTED = sorted(((t, 0xA3 + i) for i, t in enumerate(ZX_TOKENS)), key=lambda p: -len(p[0]))
_ZX_ALIASES = {"GOTO": 0xEC, "GOSUB": 0xED, "DEFFN": 0xCE, "OPEN#": 0xD3, "CLOSE#": 0xD4, "RANDOMISE": 0xF9}
_NUM_RE = re.compile(r"(\d+\.?\d*|\.\d+)(E[+-]?\d+)?", re.I)


def zx_tokenise(source: str) -> bytes:
    """BASIC text -> Spectrum program bytes (each number gets its hidden 5-byte form, as the Spectrum does)."""
    out = bytearray()
    last = -1
    for raw in source.replace("\r\n", "\n").split("\n"):
        if not raw.strip():
            continue
        m = re.match(r"\s*(\d+)\s*(.*)$", raw)
        if not m:
            raise ValueError(f"Line without a line number: {raw!r}")
        num, text = int(m.group(1)), m.group(2).rstrip()
        if num > 9999:
            raise ValueError(f"Line number {num} is too large (max 9999)")
        if num <= last:
            raise ValueError(f"Line {num} is out of order (after {last})")
        last = num
        body = _zx_tokenise_line(text, num) + b"\x0d"
        out += struct.pack(">H", num) + struct.pack("<H", len(body)) + body
    if last < 0:
        raise ValueError("No BASIC lines found")
    return bytes(out)


def _zx_tokenise_line(text: str, num: int) -> bytes:
    b = bytearray()
    i, quoted, rem = 0, False, False
    prev_ident = False               # previous char was part of a variable name (A1 is a name, not A then 1)
    bin_mode = False
    deffn = 0                        # 1 = after DEF FN, 2 = inside its parameter list
    while i < len(text):
        ch = text[i]
        if rem:
            enc, n = _zx_text(ch, text, i)
            b += enc
            i += n
            continue
        if deffn == 1 and ch == "(":
            deffn = 2
        elif deffn == 2 and ch.isalpha():            # each parameter gets a 5-byte slot for its value
            b.append(ord(ch))
            i += 1
            if i < len(text) and text[i] == "$":
                b.append(0x24)
                i += 1
            b += b"\x0e" + bytes(5)
            continue
        elif deffn == 2 and ch == ")":
            deffn = 0
        if ch == '"':
            quoted = not quoted
            b.append(0x22)
            i += 1
            prev_ident = False
            continue
        if quoted:
            enc, n = _zx_text(ch, text, i)
            b += enc
            i += n
            continue
        if ch == " ":
            if b and b[-1] >= 0xA3:          # spaces after keywords aren't stored
                i += 1
                continue
            j = i
            while j < len(text) and text[j] == " ":
                j += 1
            if _zx_keyword_at(text, j)[0] is not None:   # nor before them
                i = j
                continue
            b.append(0x20)
            i += 1
            continue
        tok, ln = _zx_keyword_at(text, i)
        if tok is not None and not prev_ident:
            while b and b[-1] == 0x20:
                b.pop()
            b.append(tok)
            i += ln
            rem = tok == 0xEA
            if rem and text[i:i + 1] == " ":
                i += 1
            bin_mode = tok == 0xC4
            if tok == 0xCE:
                deffn = 1
            prev_ident = False
            continue
        if not prev_ident and (ch.isdigit() or (ch == "." and i + 1 < len(text) and text[i + 1].isdigit())):
            if bin_mode:
                m = re.match(r"[01]+", text[i:])
                if m:
                    b += m.group().encode() + b"\x0e" + zx_float(int(m.group(), 2))
                    i += len(m.group())
                    bin_mode = False
                    continue
            m = _NUM_RE.match(text, i)
            lit = m.group()
            b += lit.upper().encode() + b"\x0e" + zx_float(float(lit))
            i += len(lit)
            continue
        bin_mode = False
        enc, n = _zx_text(ch, text, i)
        b += enc
        i += n
        prev_ident = ch.isalnum() or ch == "$" and prev_ident
    return bytes(b)


def _zx_keyword_at(text: str, i: int):
    if i >= len(text):
        return None, 0
    up = text[i:i + 10].upper()
    if i > 0 and (text[i - 1].isalpha() or text[i - 1] == "$") and up[:1].isalpha():
        return None, 0
    for t, code in list(_ZX_ALIASES.items()):
        if up.startswith(t) and not (t[-1].isalpha() and len(up) > len(t) and up[len(t)].isalpha()):
            return code, len(t)
    for t, code in _ZX_SORTED:
        if up.startswith(t):
            nxt = up[len(t):len(t) + 1]
            if t[-1].isalpha() and (nxt.isalpha() or nxt == "$"):   # whole words only (TOTAL is not TO TAL)
                continue
            if t == "SPECTRUM" or t == "PLAY":              # 128K only; leave as text in 48K listings
                continue
            return code, len(t)
    return None, 0


def _zx_text(ch: str, text: str, i: int) -> tuple[bytes, int]:
    if ch == "{":
        j = text.find("}", i)
        if j > 0:
            name = text[i + 1:j].strip()
            m = re.fullmatch(r"(?i)\$([0-9a-f]{1,2})", name)
            if m:
                return bytes([int(m.group(1), 16)]), j - i + 1
            m = re.fullmatch(r"(?i)UDG\s*([A-S])", name)
            if m:
                return bytes([0x90 + ord(m.group(1).upper()) - 65]), j - i + 1
            m = re.fullmatch(r"(?i)block\s*(\d+)", name)
            if m and int(m.group(1)) < 16:
                return bytes([0x80 + int(m.group(1))]), j - i + 1
    if ch == "£":
        return b"\x60", 1
    if ch == "©":
        return b"\x7f", 1
    if ch == "↑":
        return b"\x5e", 1
    if 0x20 <= ord(ch) < 0x7F:
        return ch.encode(), 1
    raise ValueError(f"Character {ch!r} has no Spectrum equivalent - use a {{$xx}} code")


# ============================================================================ BBC BASIC
BBC_TOKENS = {
    0x80: "AND", 0x81: "DIV", 0x82: "EOR", 0x83: "MOD", 0x84: "OR", 0x85: "ERROR", 0x86: "LINE", 0x87: "OFF",
    0x88: "STEP", 0x89: "SPC", 0x8A: "TAB(", 0x8B: "ELSE", 0x8C: "THEN", 0x8E: "OPENIN", 0x8F: "PTR",
    0x90: "PAGE", 0x91: "TIME", 0x92: "LOMEM", 0x93: "HIMEM", 0x94: "ABS", 0x95: "ACS", 0x96: "ADVAL", 0x97: "ASC",
    0x98: "ASN", 0x99: "ATN", 0x9A: "BGET", 0x9B: "COS", 0x9C: "COUNT", 0x9D: "DEG", 0x9E: "ERL", 0x9F: "ERR",
    0xA0: "EVAL", 0xA1: "EXP", 0xA2: "EXT", 0xA3: "FALSE", 0xA4: "FN", 0xA5: "GET", 0xA6: "INKEY", 0xA7: "INSTR(",
    0xA8: "INT", 0xA9: "LEN", 0xAA: "LN", 0xAB: "LOG", 0xAC: "NOT", 0xAD: "OPENUP", 0xAE: "OPENOUT", 0xAF: "PI",
    0xB0: "POINT(", 0xB1: "POS", 0xB2: "RAD", 0xB3: "RND", 0xB4: "SGN", 0xB5: "SIN", 0xB6: "SQR", 0xB7: "TAN",
    0xB8: "TO", 0xB9: "TRUE", 0xBA: "USR", 0xBB: "VAL", 0xBC: "VPOS", 0xBD: "CHR$", 0xBE: "GET$", 0xBF: "INKEY$",
    0xC0: "LEFT$(", 0xC1: "MID$(", 0xC2: "RIGHT$(", 0xC3: "STR$", 0xC4: "STRING$(", 0xC5: "EOF", 0xC6: "AUTO",
    0xC7: "DELETE", 0xC8: "LOAD", 0xC9: "LIST", 0xCA: "NEW", 0xCB: "OLD", 0xCC: "RENUMBER", 0xCD: "SAVE",
    0xCE: "EDIT", 0xCF: "PTR", 0xD0: "PAGE", 0xD1: "TIME", 0xD2: "LOMEM", 0xD3: "HIMEM", 0xD4: "SOUND",
    0xD5: "BPUT", 0xD6: "CALL", 0xD7: "CHAIN", 0xD8: "CLEAR", 0xD9: "CLOSE", 0xDA: "CLG", 0xDB: "CLS",
    0xDC: "DATA", 0xDD: "DEF", 0xDE: "DIM", 0xDF: "DRAW", 0xE0: "END", 0xE1: "ENDPROC", 0xE2: "ENVELOPE",
    0xE3: "FOR", 0xE4: "GOSUB", 0xE5: "GOTO", 0xE6: "GCOL", 0xE7: "IF", 0xE8: "INPUT", 0xE9: "LET", 0xEA: "LOCAL",
    0xEB: "MODE", 0xEC: "MOVE", 0xED: "NEXT", 0xEE: "ON", 0xEF: "VDU", 0xF0: "PLOT", 0xF1: "PRINT", 0xF2: "PROC",
    0xF3: "READ", 0xF4: "REM", 0xF5: "REPEAT", 0xF6: "REPORT", 0xF7: "RESTORE", 0xF8: "RETURN", 0xF9: "RUN",
    0xFA: "STOP", 0xFB: "COLOUR", 0xFC: "TRACE", 0xFD: "UNTIL", 0xFE: "WIDTH", 0xFF: "OSCLI"}
BBC_LOAD, BBC_EXEC = 0xFF1900, 0xFF8023          # what SAVE writes for a BASIC program on a BBC Micro


def bbc_is_basic(b: bytes) -> bool:
    return bbc_list(b) is not None


def bbc_list(b: bytes) -> str | None:
    """Listing of a tokenised BBC BASIC program, or None."""
    pos, out = 0, []
    while pos < len(b):
        if b[pos] != 0x0D or pos + 1 >= len(b):
            return None
        if b[pos + 1] == 0xFF:
            pos += 2
            break
        if pos + 4 > len(b):
            return None
        num, ln = b[pos + 1] << 8 | b[pos + 2], b[pos + 3]
        if ln < 4 or pos + ln > len(b):
            return None
        out.append(f"{num} " + _bbc_line(b[pos + 4:pos + ln]))
        pos += ln
    else:
        return None
    if not out and pos != 2:
        return None
    return "\n".join(out) + "\n" if out else ""


def _bbc_line(t: bytes) -> str:
    s, i, quoted, literal = [], 0, False, False
    while i < len(t):
        c = t[i]
        if c == 0x22:
            quoted = not quoted
            s.append('"')
        elif c == 0x8D and not quoted and not literal and i + 3 < len(t):
            b1, b2, b3 = t[i + 1:i + 4]
            lo = ((b1 << 2) & 0xC0) ^ b2
            hi = ((b1 << 4) & 0xC0) ^ b3
            s.append(str(lo | hi << 8))
            i += 3
        elif c >= 0x80 and not quoted and not literal:
            s.append(BBC_TOKENS.get(c, "{$%02x}" % c))
            if c in (0xF4, 0xDC):            # REM, DATA
                literal = True
        elif 0x20 <= c < 0x7F:
            s.append(chr(c))
        else:
            s.append("{$%02x}" % c)
        i += 1
    return "".join(s)


# keywords that are only tokenised when not followed by a letter/digit (so TIMER, PIE, ENDING stay names)
_BBC_CONDITIONAL = {"TRUE", "FALSE", "PI", "TIME", "PAGE", "LOMEM", "HIMEM", "PTR", "ERL", "ERR", "COUNT", "END",
                    "ENDPROC", "GET", "GET$", "INKEY", "NEW", "OLD", "RUN", "STOP", "VPOS", "POS", "EOF", "EXT",
                    "REPORT", "TRACE", "WIDTH", "CLG", "CLS", "TO", "OR", "ON", "OFF", "LINE", "LN", "FN", "INT"}
_BBC_PSEUDO = {"PTR": 0xCF, "PAGE": 0xD0, "TIME": 0xD1, "LOMEM": 0xD2, "HIMEM": 0xD3}
_BBC_SORTED = sorted({v: k for k, v in reversed(list(BBC_TOKENS.items()))}.items(), key=lambda p: -len(p[0]))
_BBC_LINE_AFTER = {0xE5, 0xE4, 0xF7, 0x8C, 0x8B}        # GOTO GOSUB RESTORE THEN ELSE


def _bbc_line_number(n: int) -> bytes:
    lo, hi = n & 0xFF, n >> 8
    b1 = (((lo & 0xC0) >> 2) | ((hi & 0xC0) >> 4)) ^ 0x54
    return bytes([0x8D, b1, (lo & 0x3F) | 0x40, (hi & 0x3F) | 0x40])


def bbc_tokenise(source: str) -> bytes:
    """BBC BASIC text -> tokenised program (as SAVE writes it)."""
    out = bytearray()
    last = -1
    for raw in source.replace("\r\n", "\n").split("\n"):
        if not raw.strip():
            continue
        m = re.match(r"\s*(\d+)\s?(.*)$", raw)
        if not m:
            raise ValueError(f"Line without a line number: {raw!r}")
        num, text = int(m.group(1)), m.group(2).rstrip()
        if num > 32767:
            raise ValueError(f"Line number {num} is too large (max 32767)")
        if num <= last:
            raise ValueError(f"Line {num} is out of order (after {last})")
        last = num
        body = _bbc_tokenise_line(text)
        if len(body) + 4 > 255:
            raise ValueError(f"Line {num} is too long")
        out += bytes([0x0D, num >> 8, num & 0xFF, len(body) + 4]) + body
    out += b"\x0d\xff"
    return bytes(out)


def _bbc_tokenise_line(text: str) -> bytes:
    b = bytearray()
    i, quoted, literal = 0, False, False
    stmt_start, want_line = True, False
    while i < len(text):
        ch = text[i]
        if literal:
            b += _bbc_char(ch)
            i += 1
            continue
        if ch == '"':
            quoted = not quoted
            b.append(0x22)
            i += 1
            stmt_start = False
            continue
        if quoted:
            b += _bbc_char(ch)
            i += 1
            continue
        if stmt_start and ch == "*" and not b.strip(b" "):   # *command: rest of the line is passed to the OS
            b += text[i:].encode("latin-1", "replace")
            break
        if ch == ":":
            b.append(0x3A)
            i += 1
            stmt_start, want_line = True, False
            continue
        if want_line and ch.isdigit():
            m = re.match(r"\d+", text[i:])
            b += _bbc_line_number(int(m.group()))
            i += len(m.group())
            continue
        if ch in " ,":
            b.append(ord(ch))
            i += 1
            continue
        if ch.isalpha() and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] in "_%$")):
            tok = None
            for word, code in _BBC_SORTED:
                if text.startswith(word, i):
                    nxt = text[i + len(word):i + len(word) + 1]
                    if word in _BBC_CONDITIONAL and (nxt.isalnum() or nxt == "_"):
                        continue
                    tok = (word, code)
                    break
            if tok:
                word, code = tok
                if word in _BBC_PSEUDO and stmt_start:
                    code = _BBC_PSEUDO[word]
                b.append(code)
                i += len(word)
                literal = code in (0xF4, 0xDC)
                want_line = code in _BBC_LINE_AFTER
                stmt_start = code in (0x8C, 0x8B)           # after THEN / ELSE a new statement starts
                continue
            m = re.match(r"[A-Za-z_][A-Za-z0-9_]*[%$]?", text[i:])
            b += m.group().encode()
            i += len(m.group())
            stmt_start = want_line = False
            continue
        b += _bbc_char(ch)
        i += 1
        stmt_start = False
        if not ch.isdigit():
            want_line = False
    return bytes(b)


def _bbc_char(ch: str) -> bytes:
    if ch == "£":
        return b"\x60"
    o = ord(ch)
    if o < 256:
        return bytes([o])
    raise ValueError(f"Character {ch!r} can't be stored in BBC BASIC")


# ============================================================================ helpers
def detect_listing(data: bytes, hint: str = "") -> tuple[str, str] | None:
    """Try each dialect: -> (dialect name, listing) or None. hint: 'cbm' / 'zx' / 'bbc' to try first."""
    order = ["cbm", "bbc", "zx"]
    if hint in order:
        order.remove(hint)
        order.insert(0, hint)
    for d in order:
        try:
            if d == "cbm":
                t = cbm_list(data)
                if t:
                    return "Commodore BASIC", t
            elif d == "bbc":
                t = bbc_list(data)
                if t:
                    return "BBC BASIC", t
            elif d == "zx" and hint == "zx":
                t = zx_list(data)
                if t:
                    return "Sinclair BASIC", t
        except Exception:
            continue
    return None
