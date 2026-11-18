"""Application and library settings.

Two layers:
  * App settings  (<config dir>/RetroRAG/app.json) - machine specific: last library, tesseract path, UI.
                    Windows %APPDATA%, macOS ~/Library/Application Support, Linux ~/.config
  * Library settings (<library>/library.json)      - travel with the library: OCR, chunking, embeddings, API.

The MCP / REST servers only need the library folder; everything they need is in library.json.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP_NAME = "RetroRAG"
APP_VERSION = "1.0.0"


def app_root() -> Path:
    """Folder the app lives in (the folder holding RetroRAG.exe when frozen, else the project folder)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def is_portable() -> bool:
    """Portable mode: a file called 'portable.flag' next to the app (or RETRORAG_PORTABLE=1).
    Settings then live in <app>/settings and the default library is <app>/Library, so the whole
    folder can be copied to another PC or run from a USB drive."""
    return os.environ.get("RETRORAG_PORTABLE") == "1" or (app_root() / "portable.flag").exists()


def app_dir() -> Path:
    if is_portable():
        p = app_root() / "settings"
    else:
        from .platform_util import config_base
        p = config_base() / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def default_library_path() -> Path:
    if is_portable():
        return app_root() / "Library"
    from .platform_util import documents_dir
    return documents_dir() / "RetroRAG Library"


def resolve_library_path(stored: str) -> Path:
    """In portable mode library paths are stored relative to the app folder so a drive-letter change
    (USB stick E: -> F:) or a move to another PC doesn't break them."""
    p = Path(stored)
    if not p.is_absolute():
        p = app_root() / p
    return p


def store_library_path(p: Path) -> str:
    p = Path(p).resolve()
    if is_portable():
        try:
            return os.path.relpath(p, app_root())
        except ValueError:   # different drive
            pass
    return str(p)


# --------------------------------------------------------------------------- app settings
@dataclass
class AppSettings:
    library_path: str = ""
    recent_libraries: list = field(default_factory=list)
    tesseract_path: str = ""
    window_geometry: str = ""
    window_state: str = ""
    editor_font_size: int = 11
    auto_index: bool = True              # re-index automatically after adding / OCR / saving edits
    start_servers: bool = False          # start REST + MCP HTTP servers when the app opens
    theme: str = "system"                # system | light | dark | retro
    retro_phosphor: str = "green"        # green | amber | white
    retro_scanlines: bool = True
    retro_font_file: str = ""            # optional .ttf to use instead of the built-in pixel font
    doc_split: list = field(default_factory=list)   # Document tab: form / preview splitter sizes

    @classmethod
    def load(cls) -> "AppSettings":
        f = app_dir() / "app.json"
        s = cls()
        if f.exists():
            try:
                data = json.loads(f.read_text("utf-8"))
                for fld in fields(cls):
                    if fld.name in data:
                        setattr(s, fld.name, data[fld.name])
            except Exception:
                pass
        if not s.library_path:
            s.library_path = store_library_path(default_library_path())
        return s

    def save(self) -> None:
        (app_dir() / "app.json").write_text(json.dumps(asdict(self), indent=2), "utf-8")


# --------------------------------------------------------------------------- library settings
@dataclass
class LibrarySettings:
    # OCR
    ocr_language: str = "eng"
    ocr_dpi: int = 300
    ocr_psm: int = 3                      # tesseract page segmentation mode (3 = auto, 6 = block, 11 = sparse)
    low_conf_threshold: int = 70          # words below this confidence are wrapped in {?word|NN?}
    ocr_upscale_small_images: bool = True
    ocr_drop_graphics: bool = True        # ignore drawing shapes OCR mistakes for words (schematics, pinouts)
    ocr_cleanup: str = "auto"             # auto | none | light | strong  (median denoise before OCR)
    pdf_min_text_chars: int = 25          # pages with less extractable text than this are treated as scanned
    tesseract_path: str = ""              # optional override; app setting used otherwise

    # Chunking
    chunk_chars: int = 1200
    chunk_overlap: int = 200
    code_chunk_lines: int = 60

    # Embeddings
    embed_provider: str = "builtin"       # builtin (no server) | ollama | openai | fastembed | none
    embed_model: str = "nomic-embed-text-v1.5"
    embed_url: str = ""
    embed_api_key: str = ""
    embed_doc_prefix: str = "search_document: "
    embed_query_prefix: str = "search_query: "
    embed_batch: int = 64                 # chunks per request
    embed_parallel: int = 2               # requests in flight at once
    embed_code: bool = False              # embed source-code chunks too (slow for big trees; keyword search always works)

    # Servers
    api_host: str = "127.0.0.1"           # 0.0.0.0 to let other PCs on the LAN connect (set api_token too!)
    api_port: int = 8765                  # REST + OpenAPI
    mcp_port: int = 8766                  # MCP streamable HTTP at /mcp (Unsloth Studio, LM Studio, Open WebUI...)
    api_token: str = ""                   # if set, clients must send  Authorization: Bearer <token>

    # Ingest
    extract_archives: bool = True
    strings_min_len: int = 6

    @classmethod
    def load(cls, library: Path) -> "LibrarySettings":
        f = Path(library) / "library.json"
        s = cls()
        if f.exists():
            try:
                data = json.loads(f.read_text("utf-8"))
                for fld in fields(cls):
                    if fld.name in data:
                        setattr(s, fld.name, data[fld.name])
            except Exception:
                pass
        return s

    def save(self, library: Path) -> None:
        Path(library).mkdir(parents=True, exist_ok=True)
        (Path(library) / "library.json").write_text(json.dumps(asdict(self), indent=2), "utf-8")

    def embed_signature(self) -> str:
        if self.embed_provider == "none":
            return "none"
        if self.embed_provider == "builtin":
            from .embeddings import resolve_builtin
            return f"builtin:{resolve_builtin(self.embed_model) or self.embed_model}"
        return f"{self.embed_provider}:{self.embed_model}"

    def normalise_builtin(self) -> bool:
        """Built-in provider with an Ollama-style / missing model name: store the canonical name and that model's
        prefixes. Returns True if anything changed."""
        if self.embed_provider != "builtin":
            return False
        from .embeddings import BUILTIN_MODELS, resolve_builtin
        name = resolve_builtin(self.embed_model)
        if not name or (name == self.embed_model and not self.embed_url):
            return False
        spec = BUILTIN_MODELS[name]
        self.embed_model, self.embed_url = name, ""
        self.embed_doc_prefix, self.embed_query_prefix = spec["doc_prefix"], spec["query_prefix"]
        return True


# One-click presets for the embedding settings (provider, url, model, doc prefix, query prefix)
EMBED_PRESETS = {
    "Built-in - nomic-embed-text (no server needed, recommended)": (
        "builtin", "", "nomic-embed-text-v1.5", "search_document: ", "search_query: "),
    "Built-in - bge-small (fastest, less accurate; 34 MB)": (
        "builtin", "", "bge-small-en-v1.5", "", "Represent this sentence for searching relevant passages: "),
    "Ollama - nomic-embed-text": ("ollama", "http://localhost:11434", "nomic-embed-text", "search_document: ", "search_query: "),
    "Ollama - mxbai-embed-large": ("ollama", "http://localhost:11434", "mxbai-embed-large", "",
                                   "Represent this sentence for searching relevant passages: "),
    "Ollama - snowflake-arctic-embed:33m (smaller, faster on CPU)": (
        "ollama", "http://localhost:11434", "snowflake-arctic-embed:33m", "",
        "Represent this sentence for searching relevant passages: "),
    "Ollama - embeddinggemma": ("ollama", "http://localhost:11434", "embeddinggemma", "title: none | text: ",
                                "task: search result | query: "),
    "Unsloth Studio (/v1/embeddings)": ("openai", "http://localhost:8888/v1", "default", "", ""),
    "LM Studio (/v1/embeddings)": ("openai", "http://localhost:1234/v1", "text-embedding-nomic-embed-text-v1.5",
                                   "search_document: ", "search_query: "),
    "llama.cpp server (/v1/embeddings)": ("openai", "http://localhost:8080/v1", "default", "", ""),
    "None - keyword search only": ("none", "", "", "", ""),
}

CATEGORIES = [
    "User Guides & Manuals",
    "Programming Reference",
    "Schematics",
    "Pinouts",
    "Chip Datasheets",
    "Service & Repair",
    "Books & Magazines",
    "Emulator Source",
    "Firmware & ROMs",
    "Disk & Tape Images",
    "Other",
]

SYSTEM_KINDS = ["Home computer", "Console", "Handheld", "Arcade", "Workstation", "Peripheral", "Other"]
