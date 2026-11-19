"""Adding RetroRAG to AI apps that keep their MCP servers in a settings file: opencode (https://opencode.ai).

opencode reads ~/.config/opencode/opencode.json or opencode.jsonc (on Windows too; $XDG_CONFIG_HOME/opencode when
that's set) for every project. opencode 2 lists MCP servers under "mcp" -> "servers"; opencode 1 put them straight
under "mcp" with "enabled": true, and refuses a config in the other layout, so the installed version decides.

The file may be JSONC (comments, trailing commas). One without comments is rewritten as plain JSON, keeping a .bak.
One with comments is left alone so they aren't lost: opencode's own `opencode mcp add --global` keeps them, so the
user is given that command to run instead.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

NAME = "retrorag"
SCHEMA = "https://opencode.ai/config.json"


class SetupError(Exception):
    """Couldn't add RetroRAG to the app's settings; the message says why and what to do instead."""


def opencode_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "opencode"


def opencode_config_file() -> Path:
    """The global config to edit: the existing opencode.json or opencode.jsonc, or a new opencode.json."""
    d = opencode_config_dir()
    for name in ("opencode.json", "opencode.jsonc"):
        if (d / name).is_file():
            return d / name
    return d / "opencode.json"


def opencode_cli() -> str | None:
    """The opencode command, if it's installed. GUI apps on macOS start with a short PATH, hence the usual places."""
    found = shutil.which("opencode")
    if found:
        return found
    home = Path.home()
    if os.name == "nt":
        cands = [home / ".opencode" / "bin" / "opencode.exe"]
        if os.environ.get("APPDATA"):
            cands.append(Path(os.environ["APPDATA"]) / "npm" / "opencode.cmd")
    else:
        cands = [home / ".opencode" / "bin" / "opencode", home / ".bun" / "bin" / "opencode",
                 home / ".local" / "bin" / "opencode", Path("/opt/homebrew/bin/opencode"), Path("/usr/local/bin/opencode")]
    return next((str(c) for c in cands if c.is_file()), None)


def opencode_version(cli: str | None = None) -> tuple[int, int, int] | None:
    cli = cli or opencode_cli()
    if not cli:
        return None
    kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        out = subprocess.run([cli, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                             stdin=subprocess.DEVNULL, timeout=20, **kw).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out or "")
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def parse_jsonc(text: str) -> tuple[object, bool]:
    """(the parsed value, whether the text had comments). Allows // and /* */ comments and trailing commas."""
    text = text.lstrip("﻿")
    out: list[str] = []
    comments = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            comments = True
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
            comments = True
        else:
            if c in "}]":                                   # drop a trailing comma before it
                k = len(out) - 1
                while k >= 0 and out[k].isspace():
                    k -= 1
                if k >= 0 and out[k] == ",":
                    del out[k]
            out.append(c)
            i += 1
    clean = "".join(out)
    return (json.loads(clean) if clean.strip() else {}), comments


def opencode_entry(cmd: str, args: list[str], env: dict, legacy: bool = False) -> dict:
    e: dict = {"type": "local", "command": [cmd, *args]}
    if env:
        e["environment"] = dict(env)
    if legacy:
        e["enabled"] = True
    return e


def opencode_snippet(cmd: str, args: list[str], env: dict) -> str:
    """What to paste into opencode.json by hand (opencode 2)."""
    return json.dumps({"$schema": SCHEMA, "mcp": {"servers": {NAME: opencode_entry(cmd, args, env)}}}, indent=2)


def _q(s: str) -> str:
    return f'"{s}"' if (not s or any(ch in s for ch in " &()^;'")) else s


def opencode_command(cmd: str, args: list[str], env: dict) -> str:
    """`opencode mcp add` for this PC's stdio server, for a terminal (it keeps comments in the config).
    The "--" is quoted because PowerShell drops a bare -- on its way to opencode.ps1 (the npm install's launcher)."""
    return " ".join(["opencode mcp add --global", NAME] + [f'--env {k}="{v}"' for k, v in env.items()]
                    + ['"--"', f'"{cmd}"'] + [_q(a) for a in args])


def opencode_http_command(url: str, token: str = "") -> str:
    """`opencode mcp add` for the HTTP server, for opencode on another PC."""
    return f"opencode mcp add --global {NAME} --url {url}" + (f' --header "Authorization=Bearer {token}"' if token else "")


def _is_legacy(cfg: dict, version: tuple | None) -> bool:
    if version:
        return version[0] < 2
    mcp = cfg.get("mcp")                                    # no opencode command to ask: go by what's in the file
    return isinstance(mcp, dict) and any(isinstance(v, dict) and "type" in v
                                         for k, v in mcp.items() if k not in ("servers", "timeout"))


def install_opencode(cmd: str, args: list[str], env: dict) -> tuple[Path, Path | None]:
    """Add (or update) RetroRAG in opencode's global config, for every project. Returns (the file written, its
    backup, or None when the file is new)."""
    path = opencode_config_file()
    try:
        text = path.read_text("utf-8") if path.exists() else ""
    except OSError as e:
        raise SetupError(f"Couldn't read {path}: {e}")
    try:
        cfg, comments = parse_jsonc(text)
    except ValueError as e:
        raise SetupError(f"{path} isn't valid JSON ({e}), so RetroRAG didn't change it.")
    if not isinstance(cfg, dict) or not isinstance(cfg.get("mcp", {}), dict):
        raise SetupError(f"{path} isn't laid out as opencode expects, so RetroRAG didn't change it.")
    if comments:
        raise SetupError(f"{path} has comments in it, and RetroRAG would lose them by rewriting it.")
    legacy = _is_legacy(cfg, opencode_version())
    entry = opencode_entry(cmd, args, env, legacy)
    if not text.strip():
        cfg["$schema"] = SCHEMA
    mcp = cfg.setdefault("mcp", {})
    if legacy:
        mcp[NAME] = entry
    else:
        servers = mcp.setdefault("servers", {})
        if not isinstance(servers, dict):
            raise SetupError(f"{path} isn't laid out as opencode expects, so RetroRAG didn't change it.")
        servers[NAME] = entry
    bak = path.with_name(path.name + ".bak") if path.exists() else None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if bak:
            shutil.copy2(path, bak)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(cfg, indent=2) + "\n", "utf-8")
        os.replace(tmp, path)
    except OSError as e:
        raise SetupError(f"Couldn't write {path}: {e}")
    return path, bak
