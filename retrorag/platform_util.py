"""Small OS-specific helpers so RetroRAG runs the same on Windows, macOS and Linux (incl. Raspberry Pi OS)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = not IS_WINDOWS and not IS_MAC


def config_base() -> Path:
    """Where per-user app settings live: %APPDATA% / ~/Library/Application Support / $XDG_CONFIG_HOME."""
    if IS_WINDOWS:
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    if IS_MAC:
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def documents_dir() -> Path:
    if IS_LINUX:
        try:
            out = subprocess.run(["xdg-user-dir", "DOCUMENTS"], capture_output=True, text=True, timeout=3).stdout.strip()
            if out and Path(out).is_dir() and Path(out) != Path.home():
                return Path(out)
        except Exception:
            pass
    docs = Path.home() / "Documents"
    return docs if docs.is_dir() else Path.home()


def open_path(p: str | Path) -> None:
    """Open a file or folder with its default application."""
    p = str(p)
    if IS_WINDOWS:
        os.startfile(p)  # noqa: S606 - Windows only
    elif IS_MAC:
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def reveal(p: str | Path) -> None:
    """Show a file in the system file manager, selected where the platform supports it."""
    p = Path(p)
    if IS_WINDOWS:
        subprocess.Popen(["explorer", "/select,", str(p)])
    elif IS_MAC:
        subprocess.Popen(["open", "-R", str(p)])
    else:
        # freedesktop FileManager1 (Nautilus, Dolphin, Nemo, Caja, Thunar...) selects the file; else open its folder
        try:
            r = subprocess.run(["dbus-send", "--session", "--print-reply", "--dest=org.freedesktop.FileManager1",
                                "/org/freedesktop/FileManager1", "org.freedesktop.FileManager1.ShowItems",
                                f"array:string:{p.resolve().as_uri()}", "string:"],
                               capture_output=True, timeout=3)
            if r.returncode == 0:
                return
        except Exception:
            pass
        open_path(p.parent)


def is_network_path(p: str | Path) -> bool:
    """True for UNC paths and mapped network drives (Windows), or NFS/SMB/CIFS mounts (Linux/macOS)."""
    s = str(Path(p).resolve())
    if s.startswith("\\\\"):
        return True
    if IS_WINDOWS:
        try:
            import ctypes
            return ctypes.windll.kernel32.GetDriveTypeW(s[:3]) == 4      # DRIVE_REMOTE
        except Exception:
            return False
    try:
        best, fstype = "", ""
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) > 2 and s.startswith(parts[1]) and len(parts[1]) > len(best):
                    best, fstype = parts[1], parts[2]
        return fstype.startswith(("nfs", "cifs", "smb", "fuse.sshfs"))
    except OSError:
        return False


def lan_address() -> str | None:
    """This PC's address on the local network (what another PC uses to reach it), or None."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))          # picks the outgoing interface; nothing is sent
            ip = s.getsockname()[0]
        return None if ip.startswith(("127.", "0.")) else ip
    except OSError:
        return None


def file_manager_name() -> str:
    return "Explorer" if IS_WINDOWS else "Finder" if IS_MAC else "file manager"


def exe_name(base: str) -> str:
    return base + ".exe" if IS_WINDOWS else base


# ---------------------------------------------------------------- Tesseract
def tesseract_candidates() -> list[str]:
    """Places Tesseract is commonly installed. GUI apps on macOS start with a minimal PATH, hence Homebrew paths."""
    c = [shutil.which("tesseract") or ""]
    if IS_WINDOWS:
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", ""),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
            if base:
                c.append(os.path.join(base, "Tesseract-OCR", "tesseract.exe"))
    elif IS_MAC:
        c += ["/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract", "/opt/local/bin/tesseract"]
    else:
        c += ["/usr/bin/tesseract", "/usr/local/bin/tesseract", "/snap/bin/tesseract"]
    return c


def tesseract_install_command() -> str:
    if IS_WINDOWS:
        return "winget install UB-Mannheim.TesseractOCR"
    if IS_MAC:
        return "brew install tesseract"
    return "sudo apt install tesseract-ocr"


def tesseract_install_hint() -> str:
    extra = ("(or from https://github.com/UB-Mannheim/tesseract/wiki)" if IS_WINDOWS else
             "(extra languages: brew install tesseract-lang)" if IS_MAC else
             "(Fedora: sudo dnf install tesseract; extra languages: tesseract-ocr-deu, -fra, ...)")
    return ("Tesseract OCR is not installed or not found.\n"
            f"Install it with:  {tesseract_install_command()}\n{extra}\n"
            "then set its path in Settings if needed.")


# ---------------------------------------------------------------- Claude Desktop
def claude_desktop_config_paths() -> list[Path]:
    """Candidate claude_desktop_config.json locations, most likely first."""
    out: list[Path] = []
    if IS_WINDOWS:
        appdata = os.environ.get("APPDATA")
        if appdata:
            out.append(Path(appdata) / "Claude" / "claude_desktop_config.json")
        local = os.environ.get("LOCALAPPDATA")
        if local and (Path(local) / "Packages").exists():
            for d in (Path(local) / "Packages").glob("Claude_*"):
                out.append(d / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json")
    elif IS_MAC:
        out.append(Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json")
    else:
        out.append(config_base() / "Claude" / "claude_desktop_config.json")
    return out
