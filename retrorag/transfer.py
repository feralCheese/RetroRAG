"""Transfer area: files passing between an AI chat and RetroRAG without going into the library.

Chat apps don't hand attached files to tools, so files travel by link instead:

  in   get_upload_link() gives a one-off page, /transfer/<id>/upload. The user drops files there in a browser (or
       an AI that can run code PUTs them to /transfer/<id>/upload/<name>). Zips are unpacked.
  out  new images and extracted files are saved to an area and served at /transfer/<id>/<file>.

Areas live in <library>/_transfer/<id>/. The id is long and random, so a link is its own permission (no access
token needed). Upload pages close after UPLOAD_HOURS, and areas are deleted after KEEP_HOURS.
"""
from __future__ import annotations

import json
import re
import secrets
import shutil
import socket
import time
from pathlib import Path

from .library import Library

TRANSFER_DIR = "_transfer"
UPLOAD_HOURS = 2
KEEP_HOURS = 24
MAX_FILE = 64 << 20               # one uploaded file
MAX_UNZIPPED = 256 << 20          # everything unpacked from one zip
MAX_MEMBERS = 5000
_ID = re.compile(r"^[A-Za-z0-9_-]{12,40}$")


class TransferError(ValueError):
    pass


# ---------------------------------------------------------------- areas
def _root(lib: Library) -> Path:
    return lib.root / TRANSFER_DIR


def _meta(p: Path) -> dict:
    try:
        return json.loads((p / ".meta.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def cleanup(lib: Library) -> None:
    root = _root(lib)
    if not root.is_dir():
        return
    now = time.time()
    for p in root.iterdir():
        if p.is_dir() and now - _meta(p).get("created", 0) > KEEP_HOURS * 3600:
            shutil.rmtree(p, ignore_errors=True)


def new_area(lib: Library, kind: str) -> tuple[str, Path]:
    cleanup(lib)
    tid = secrets.token_urlsafe(16)
    p = _root(lib) / tid
    p.mkdir(parents=True)
    (p / ".meta.json").write_text(json.dumps({"kind": kind, "created": time.time()}), "utf-8")
    readme = _root(lib) / "README.txt"
    if not readme.exists():
        readme.write_text("Temporary files passed between AI chats and RetroRAG (uploads, new images, extracted\n"
                          f"files). Each folder is deleted {KEEP_HOURS} hours after it was made. Safe to delete.\n",
                          "utf-8")
    return tid, p


def area(lib: Library, tid: str) -> Path:
    tid = (tid or "").strip().strip("/")
    p = _root(lib) / tid
    if not _ID.match(tid) or not p.is_dir():
        raise TransferError(f"No upload or download area {tid!r} (they're deleted after {KEEP_HOURS} hours)")
    if time.time() - _meta(p).get("created", 0) > KEEP_HOURS * 3600:
        raise TransferError(f"That area has expired (they last {KEEP_HOURS} hours)")
    return p


def upload_open(lib: Library, tid: str) -> bool:
    try:
        p = area(lib, tid)
    except TransferError:
        return False
    m = _meta(p)
    return m.get("kind") == "upload" and time.time() - m.get("created", 0) < UPLOAD_HOURS * 3600


def _safe_rel(rel: str) -> str:
    parts = [re.sub(r'[<>:"\\|?*\x00-\x1f]', "_", s).strip().rstrip(".") for s in rel.replace("\\", "/").split("/")]
    parts = [s for s in parts if s and s not in (".", "..")]
    if not parts:
        raise TransferError("A file needs a name")
    return "/".join(parts)


def resolve(lib: Library, ref: str) -> Path:
    """'<id>/<file>' -> its path. '<id>' alone is allowed when the area holds exactly one file."""
    ref = (ref or "").strip().strip("/")
    tid, _, rel = ref.partition("/")
    p = area(lib, tid)
    if not rel:
        files = [f for f in p.rglob("*") if f.is_file() and f.name != ".meta.json"]
        if len(files) == 1:
            return files[0]
        raise TransferError(f"Upload {tid} holds {len(files)} files - say which, e.g. {tid}/<name> (see list_uploads)")
    target = (p / _safe_rel(rel)).resolve()
    if p.resolve() not in target.parents or not target.is_file():
        raise TransferError(f"No file {rel!r} in upload {tid}")
    return target


def ref_of(lib: Library, path: Path) -> str:
    rel = path.resolve().relative_to(_root(lib).resolve())
    return rel.as_posix()


# ---------------------------------------------------------------- saving
def save_file(lib: Library, tid: str, name: str, data: bytes) -> list[str]:
    """Store one uploaded file (unpacking a zip or tarball; a .gz / .bz2 / .xz becomes what's inside it).
    -> refs of what was saved."""
    p = area(lib, tid)
    if len(data) > MAX_FILE:
        raise TransferError(f"{name} is too big ({len(data) >> 20} MB, limit {MAX_FILE >> 20} MB)")
    rel = _safe_rel(name.replace("\\", "/").split("/")[-1])
    from . import archives
    k = archives.kind_of_bytes(rel, data)
    if k == archives.ONE:
        try:
            data = archives.decompress(data, MAX_UNZIPPED)
        except ValueError as e:
            raise TransferError(f"{name} is too big: {e}")
        rel = _safe_rel(archives.stem(rel))
        k = None
    if k:
        return _unpack(lib, p, archives.stem(rel) or k, data, k)
    target = p / rel
    n = 2
    while target.exists():
        target = p / f"{Path(rel).stem} ({n}){Path(rel).suffix}"
        n += 1
    target.write_bytes(data)
    return [ref_of(lib, target)]


def _unpack(lib: Library, p: Path, folder: str, data: bytes, k: str) -> list[str]:
    """An uploaded .zip or tarball: its files go in a folder named after it."""
    from . import archives
    try:
        files = archives.extract(data, p / _safe_rel(folder), k=k, max_files=MAX_MEMBERS, max_bytes=MAX_UNZIPPED,
                                 skip=lambda n: n.startswith("__MACOSX/"), log=lambda m: None, strip_top=folder)
    except ValueError as e:
        raise TransferError(f"The {archives.label(k).lower()} archive is too big: {e}")
    return [ref_of(lib, f) for f in files]


def save_output(lib: Library, tid: str, name: str, data: bytes) -> Path:
    p = area(lib, tid)
    target = p / _safe_rel(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


# ---------------------------------------------------------------- links
def base_url(lib: Library) -> str | None:
    """Address of the running REST server (which serves the links), or None if it isn't running."""
    st = lib.settings
    host = st.api_host or "127.0.0.1"
    probe = "127.0.0.1" if host in ("0.0.0.0", "") else host
    try:
        with socket.create_connection((probe, st.api_port), timeout=0.4):
            pass
    except OSError:
        return None
    if host in ("0.0.0.0", ""):
        from .platform_util import lan_address
        host = lan_address() or "127.0.0.1"
    return f"http://{host}:{st.api_port}"


def file_info(lib: Library, path: Path, base: str | None = "?") -> dict:
    base = base_url(lib) if base == "?" else base
    ref = ref_of(lib, path)
    info = {"name": path.name, "size": path.stat().st_size, "ref": ref, "path_on_retrorag_pc": str(path)}
    if base:
        from urllib.parse import quote
        info["download_url"] = f"{base}/{TRANSFER_DIR.strip('_')}/{quote(ref)}"
    return info


def not_running_note(lib: Library) -> str:
    return ("RetroRAG's HTTP servers aren't running, so there are no download links: the files are on the "
            "RetroRAG PC at the paths shown. Start the servers (RetroRAG toolbar: Start servers) to get links.")


def upload_link(lib: Library) -> dict:
    base = base_url(lib)
    if base is None:
        raise TransferError("RetroRAG's HTTP servers aren't running, so there's nowhere to upload to. Ask the user "
                            "to click Start servers in the RetroRAG app (or run 'retrorag-cli api'), or to add the "
                            "file to the library themselves (drag it onto a system in RetroRAG).")
    tid, _ = new_area(lib, "upload")
    url = f"{base}/transfer/{tid}/upload"
    return {"upload_id": tid, "upload_page": url, "expires_in_hours": UPLOAD_HOURS,
            "how": f"Give the user this link: they open it in a browser and drop their file(s) on the page (zips "
                   f"are unpacked). Or, if you can run code with the file, send it with an HTTP PUT to "
                   f"{url}/<filename>. Then call list_uploads('{tid}')."}


def list_area(lib: Library, tid: str) -> list[dict]:
    from . import media
    p = area(lib, tid)
    base = base_url(lib)
    out = []
    for f in sorted(x for x in p.rglob("*") if x.is_file() and x.name != ".meta.json"):
        info = file_info(lib, f, base)
        fmt = None
        if f.stat().st_size <= 8 << 20:
            try:
                fmt = media.detect(f.read_bytes(), f.suffix)
            except Exception:
                fmt = None
        info["upload"] = info.pop("ref")
        if fmt:
            info["disk_or_tape_format"] = fmt.id
        out.append(info)
    return out


# ---------------------------------------------------------------- the upload page
UPLOAD_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Send files to RetroRAG</title><style>
body{font-family:system-ui,Segoe UI,sans-serif;background:#f3f4f7;color:#1c1f26;margin:0;padding:32px}
main{max-width:620px;margin:auto;background:#fff;border:1px solid #e3e6ec;border-radius:12px;padding:28px}
h1{font-size:22px;margin:0 0 6px} p{color:#5b6272;line-height:1.5}
#drop{border:2px dashed #cdd2db;border-radius:10px;padding:44px 16px;text-align:center;margin:18px 0;cursor:pointer}
#drop.over{border-color:#4f6bed;background:#eef1ff} li{margin:4px 0} .ok{color:#15803d} .err{color:#d63c3c}
</style></head><body><main><h1>Send files to RetroRAG</h1>
<p>Drop disk or tape images, zips or any other files here. Zips are unpacked. When they're done, go back to the
chat and tell the AI.</p>
<div id="drop">Drop files here, or click to choose<input id="pick" type="file" multiple hidden></div>
<ul id="list"></ul><p style="font-size:13px">This page stops accepting files __HOURS__ hours after it was made.</p>
</main><script>
const drop=document.getElementById('drop'),pick=document.getElementById('pick'),list=document.getElementById('list');
drop.onclick=()=>pick.click();pick.onchange=()=>send(pick.files);
drop.ondragover=e=>{e.preventDefault();drop.classList.add('over')};drop.ondragleave=()=>drop.classList.remove('over');
drop.ondrop=e=>{e.preventDefault();drop.classList.remove('over');send(e.dataTransfer.files)};
async function send(files){for(const f of files){const li=document.createElement('li');li.textContent=f.name+' ...';
list.appendChild(li);try{const r=await fetch('upload/'+encodeURIComponent(f.name),{method:'PUT',body:f});
const j=await r.json();if(!r.ok)throw new Error(j.detail||r.status);li.className='ok';
li.textContent='\\u2713 '+f.name+(j.saved.length>1?' ('+j.saved.length+' files unpacked)':'');}
catch(e){li.className='err';li.textContent='\\u2717 '+f.name+': '+e.message;}}}
</script></body></html>"""


def upload_page() -> str:
    return UPLOAD_PAGE.replace("__HOURS__", str(UPLOAD_HOURS))
