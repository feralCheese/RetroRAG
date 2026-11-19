"""Links on systems and peripherals, and copies of what they point to.

A link is either just saved (the AI is told about it; RetroRAG checks now and then that it still works) or copied
into the library, so it can be searched, works offline and with local AIs, and survives the site disappearing:

    page   the page itself (or a PDF / text file the link points straight at)
    site   the page and the pages under it on the same site (same address prefix), up to a page limit, plus the
           PDFs and text files those pages link to on that site
    repo   a GitHub repository (one zip download of a branch, tag or commit), imported like a source tree

Copying is polite: one request a second (or the site's Crawl-delay), robots.txt is respected, and nothing outside
the site is fetched. Each copied file keeps its address as its source, and re-copying updates only what changed.

Every link has a reminder period (refresh_days). A link that can't be reached is retried a day later; one whose
page is gone (404 / 410, or unreachable on attempts at least GONE_AFTER_DAYS apart) is marked "gone": the copy stays
in the library and it stops counting towards "links to re-check".
"""
from __future__ import annotations

import hashlib
import io
import re
import shutil
import tempfile
import time
import urllib.robotparser
import zipfile
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

from .config import APP_VERSION
from .library import Library, Link, now_iso, parse_iso, safe_filename

UA = f"RetroRAG/{APP_VERSION} (+https://github.com/feralCheese/RetroRAG)"
ROBOTS_AGENT = "RetroRAG"
DELAY = 1.0                    # seconds between requests to a site
MAX_DELAY = 10.0               # cap on a site's Crawl-delay
MAX_DEPTH = 6                  # site copies: links followed from the first page
MAX_FILE = 64 << 20            # one page / file
MAX_REPO = 400 << 20           # a repository zip
MAX_REPO_FILES = 40000
GONE_AFTER_DAYS = 7            # unreachable on attempts this far apart -> gone
REFRESH_CHOICES = [(30, "month"), (90, "3 months"), (180, "6 months"), (365, "year"), (0, "never")]

PAGE_TYPES = ("text/html", "application/xhtml+xml")
FILE_EXT = {".pdf", ".txt", ".asc", ".doc", ".docx", ".rtf", ".xls", ".xlsx", ".md", ".nfo", ".1st", ".diz",
            ".asm", ".s", ".a65", ".bas", ".c", ".h", ".inc", ".src", ".lst", ".csv"}
PAGE_EXT = {"", ".htm", ".html", ".xhtml", ".shtml", ".php", ".php3", ".asp", ".aspx", ".jsp", ".cgi", ".pl", ".cfm"}
SKIP_URL = re.compile(r"(?i)([?&](action|oldid|diff|printable|replytocom|share|sort|order|redirect)=|"
                      r"/(special|talk|user|user_talk|file|template|template_talk)[:%]|"
                      r"/(login|logout|signin|sign_in|signup|register|search|cart|account|wp-admin|wp-login\.php|"
                      r"feed|xmlrpc\.php)(/|$|\?)|\.(rss|atom)$)")
GITHUB_NOT_OWNERS = {"features", "topics", "marketplace", "settings", "orgs", "users", "sponsors", "about",
                     "pricing", "login", "explore", "collections", "trending", "apps", "enterprise", "search",
                     "notifications", "new", "codespaces", "issues", "pulls", "readme", "security", "customer-stories"}
_GH = re.compile(r"^https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?"
                 r"(?:/(tree|blob|commit|releases/tag)/([^/?#]+)(?:/([^?#]*))?)?/?(?:[?#].*)?$", re.I)


class LinkError(Exception):
    """Can't copy it as it stands (too big, not readable, no such branch...): the link needs editing."""


class Gone(LinkError):
    """Not there any more (404 / 410, repository deleted)."""


class Unreachable(LinkError):
    """Couldn't connect or the site is having problems - try again later."""


class Refused(LinkError):
    """The site's robots.txt asks programs like RetroRAG not to fetch it."""


# ======================================================================== addresses
def github_repo(url: str) -> dict | None:
    """'https://github.com/owner/repo[/tree/<ref>[/path]]' -> {owner, repo, ref, path, blob}; None if not a repo."""
    m = _GH.match((url or "").strip())
    if not m or m.group(1).lower() in GITHUB_NOT_OWNERS:
        return None
    kind = (m.group(3) or "").lower()
    return {"owner": m.group(1), "repo": m.group(2), "ref": unquote(m.group(4) or ""),
            "path": unquote((m.group(5) or "").strip("/")), "blob": kind == "blob"}


def guess_mode(url: str) -> str:
    gh = github_repo(url)
    return "repo" if gh and not gh["blob"] else "page"


def raw_github(url: str) -> str:
    """A github.com '/blob/' page is mostly JavaScript: fetch the raw file instead."""
    gh = github_repo(url)
    if gh and gh["blob"] and gh["ref"] and gh["path"]:
        return (f"https://raw.githubusercontent.com/{gh['owner']}/{gh['repo']}/{quote(gh['ref'], safe='')}/"
                f"{quote(gh['path'])}")
    return url


def _norm(url: str) -> str:
    p = urlsplit(url)
    path = p.path or "/"
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, p.query, ""))


def scope_prefix(url: str) -> str:
    """Site copies take the pages whose address starts with this: the start page's folder."""
    p = urlsplit(_norm(url))
    folder = p.path if p.path.endswith("/") else p.path.rsplit("/", 1)[0] + "/"
    return urlunsplit((p.scheme, p.netloc, folder, "", ""))


def _ext(url: str) -> str:
    name = urlsplit(url).path.rsplit("/", 1)[-1]
    return ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""


def _file_name(url: str, ctype: str, is_page: bool) -> str:
    p = urlsplit(url)
    name = unquote(p.path.rstrip("/").rsplit("/", 1)[-1]) or "index"
    if p.query:
        q = re.sub(r"[^A-Za-z0-9._-]+", "-", unquote(p.query)).strip("-")[:48]
        name += "_" + (q or hashlib.sha1(p.query.encode()).hexdigest()[:8])
    stem, dot, ext = name.rpartition(".")
    if is_page:
        if not dot or "." + ext.lower() not in {".htm", ".html", ".xhtml"}:
            name += ".html"
    elif not dot:
        name += {"application/pdf": ".pdf", "text/plain": ".txt", "text/markdown": ".md"}.get(ctype, "")
    name = safe_filename(name)
    if len(name) > 120:
        stem, _, ext = name.rpartition(".")
        name = stem[:110] + "." + ext if stem else name[:120]
    return name


def _group_for(url: str) -> str:
    """'https://host/docs/a/page.html' -> 'host/docs/a' (folders in the library tree)."""
    p = urlsplit(url)
    parts = [safe_filename(p.netloc)] + [safe_filename(unquote(s))[:60] for s in p.path.split("/")[:-1] if s]
    return "/".join(parts)


# ======================================================================== fetching
class _Resp:
    def __init__(self, status: int, headers, url: str, content: bytes):
        self.status, self.headers, self.url, self.content = status, headers, url, content

    @property
    def ctype(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    @property
    def text(self) -> str:
        return decode_page(self.content, self.headers.get("content-type", ""))


def decode_page(data: bytes, content_type: str = "") -> str:
    m = re.search(r"charset=['\"]?([\w-]+)", content_type, re.I) or \
        re.search(rb"<meta[^>]+charset=['\"]?([\w-]+)", data[:4096], re.I)
    enc = m.group(1) if m else None
    if isinstance(enc, bytes):
        enc = enc.decode("ascii", "replace")
    for e in ([enc] if enc else []) + ["utf-8"]:
        try:
            return data.decode(e)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("cp1252", errors="replace")


def _client():
    import httpx
    return httpx.Client(headers={"User-Agent": UA}, follow_redirects=True,
                        timeout=httpx.Timeout(30, connect=15))


def _why(e: Exception, host: str) -> str:
    s = str(e)
    low = s.lower()
    if any(w in low for w in ("getaddrinfo", "name or service not known", "nodename nor servname",
                              "name resolution", "no address associated", "11001")):
        return f"There's no site called {host} any more (or this PC is offline)"
    if "refused" in low:
        return f"{host} refused the connection"
    if "ssl" in low or "certificate" in low:
        return f"{host}'s secure connection failed (certificate problem)"
    return f"Couldn't connect to {host}: {s[:160]}"


def fetch(cl, url: str, *, limit: int = MAX_FILE, headers: dict | None = None, body: bool = True) -> _Resp:
    """GET a URL. Network problems raise Unreachable; HTTP errors come back as a status for the caller to judge."""
    import httpx
    host = urlsplit(url).netloc
    for attempt in range(2):
        try:
            with cl.stream("GET", url, headers=headers or {}) as r:
                if r.status_code in (429, 503) and attempt == 0:
                    try:
                        wait = int(r.headers.get("retry-after", "8"))
                    except ValueError:
                        wait = 8
                    time.sleep(min(30, max(1, wait)))
                    continue
                if not body:
                    return _Resp(r.status_code, r.headers, str(r.url), b"")
                n = int(r.headers.get("content-length") or 0)
                if n > limit:
                    raise LinkError(f"{url} is too big to copy ({n >> 20} MB; the limit is {limit >> 20} MB)")
                buf = bytearray()
                for chunk in r.iter_bytes():
                    buf += chunk
                    if len(buf) > limit:
                        raise LinkError(f"{url} is too big to copy (over {limit >> 20} MB)")
                return _Resp(r.status_code, r.headers, str(r.url), bytes(buf))
        except httpx.TimeoutException:
            raise Unreachable(f"{host} didn't answer in time")
        except httpx.ConnectError as e:
            raise Unreachable(_why(e, host))
        except httpx.HTTPError as e:
            raise Unreachable(_why(e, host))
    raise Unreachable(f"{host} is too busy right now (it asked to come back later)")


class _Robots:
    """robots.txt rules per site (RFC 9309: a missing file allows everything)."""

    def __init__(self, cl):
        self.cl = cl
        self.cache: dict[str, urllib.robotparser.RobotFileParser] = {}

    def _get(self, url: str):
        p = urlsplit(url)
        key = f"{p.scheme}://{p.netloc}"
        rp = self.cache.get(key)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = fetch(self.cl, key + "/robots.txt", limit=512 << 10)
                if r.status == 200 and "html" not in r.ctype:
                    rp.parse(r.text.splitlines())
                else:
                    rp.allow_all = True
            except LinkError:
                rp.allow_all = True
            self.cache[key] = rp
        return rp

    def allowed(self, url: str) -> bool:
        return self._get(url).can_fetch(ROBOTS_AGENT, url)

    def delay(self, url: str) -> float:
        try:
            d = self._get(url).crawl_delay(ROBOTS_AGENT)
        except Exception:
            d = None
        return max(DELAY, min(float(d or 0), MAX_DELAY))


# ======================================================================== storing copies
class _Store:
    """Puts fetched files into the library under the link's system and category, keyed by their address:
    re-copying updates changed files and leaves unchanged ones alone."""

    def __init__(self, lib: Library, link: Link, log):
        self.lib, self.link, self.log = lib, link, log
        self.system = lib.system(link.system_id)
        default = "Emulator Source" if link.mode == "repo" else "User Guides & Manuals"
        self.category = (link.category or "").strip() or default
        self.tmp = Path(tempfile.mkdtemp(prefix="retrorag-copy-"))
        self.counts = {"added": 0, "updated": 0, "same": 0, "duplicate": 0, "removed": 0}
        self.seen: set[str] = set()
        self.pages = 0

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def save_bytes(self, data: bytes, name: str, group: str, source_url: str, title: str | None) -> int | None:
        d = self.tmp / f"{len(self.seen)}"
        d.mkdir(parents=True, exist_ok=True)
        src = d / name
        src.write_bytes(data)
        return self.save_file(src, group, source_url, title)

    def save_file(self, src: Path, group: str, source_url: str, title: str | None) -> int | None:
        from . import binaries, ingest
        lib = self.lib
        self.seen.add(source_url)
        r = lib.conn().execute("SELECT id FROM documents WHERE link_id=? AND source_url=?",
                               (self.link.id, source_url)).fetchone()
        if r:
            d = lib.document(r["id"])
            h = binaries.file_hashes(src)
            if h["sha1"] == d.sha1:
                self.counts["same"] += 1
                return d.id
            dest = lib.abspath(d.path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            upd = {"size": src.stat().st_size, "sha1": h["sha1"], "md5": h["md5"], "crc32": h["crc32"],
                   "text_updated": now_iso()}
            if title:
                upd["title"] = title
            kind = ingest.classify(dest)
            if kind != d.kind:
                upd["kind"] = kind
            lib.update_document(d.id, **upd)
            ingest.extract(lib, lib.document(d.id), log=self.log)
            self.counts["updated"] += 1
            return d.id
        doc_id = ingest.add_file(lib, self.system, self.category, src, copy=True, group_path=group, title=title or None,
                                 log=lambda m: None)
        if not doc_id:
            self.counts["duplicate"] += 1        # the same file is already in this system's library
            return None
        lib.update_document(doc_id, source_url=source_url, link_id=self.link.id)
        self.counts["added"] += 1
        return doc_id

    def remove_missing(self) -> None:
        """Repository copies mirror the repository: drop files it no longer has."""
        for r in self.lib.conn().execute("SELECT id, source_url FROM documents WHERE link_id=?",
                                         (self.link.id,)).fetchall():
            if r["source_url"] not in self.seen:
                self.lib.delete_document(r["id"], delete_files=True)
                self.counts["removed"] += 1

    def summary(self, what: str) -> str:
        c = self.counts
        parts = [f"{c['added']} new", f"{c['updated']} changed"]
        if c["removed"]:
            parts.append(f"{c['removed']} removed")
        if c["duplicate"]:
            parts.append(f"{c['duplicate']} already in the library")
        return f"{what}: " + ", ".join(parts) + f", {c['same']} unchanged"


# ======================================================================== pages and sites
def _js_only(page_text: str, scripts: int) -> bool:
    body = re.sub(r"(?m)^#.*$", "", page_text).strip()
    return len(body) < 120 and scripts > 0


def _copy_pages(lib: Library, link: Link, store: _Store, site: bool, progress, log) -> str:
    from .htmltext import parse_html
    start = _norm(raw_github(link.url))
    prefix = scope_prefix(start)
    host = urlsplit(start).netloc
    limit = max(1, int(link.max_pages or 100)) if site else 1
    with _client() as cl:
        robots = _Robots(cl)
        if not robots.allowed(start):
            raise Refused(f"{host} asks programs like RetroRAG not to copy this page (its robots.txt). "
                          "The link is kept; you could save the page from your browser and add the file instead.")
        delay = robots.delay(start)
        queue: deque = deque([(start, 0, True)])
        queued = {start}
        done = 0
        first = True
        while queue and done < limit:
            url, depth, is_page_link = queue.popleft()
            if not first:
                if not robots.allowed(url):
                    continue
                time.sleep(delay)
            if progress:
                progress(done, limit, url)
            try:
                r = fetch(cl, url)
            except Unreachable:
                if first:
                    raise
                log(f"  couldn't fetch {url}")
                continue
            except LinkError as e:
                if first:
                    raise
                log(f"  skipped: {e}")
                continue
            if first and r.status in (404, 410):
                raise Gone(f"The page isn't there any more ({r.status} {'Not Found' if r.status == 404 else 'Gone'})")
            if first and r.status in (401, 403):
                raise LinkError(f"{host} won't let RetroRAG read this page ({r.status}) - it may need you to "
                                "sign in. Save it from your browser and add the file instead.")
            if first and r.status >= 500:
                raise Unreachable(f"{host} is having problems ({r.status})")
            if r.status >= 400:
                log(f"  {r.status}: {url}")
                first = False
                continue
            final = _norm(r.url)
            is_page = r.ctype in PAGE_TYPES or (not r.ctype and _ext(final) in PAGE_EXT)
            if is_page:
                pg = parse_html(r.text)
                if pg.refresh and len(pg.text) < 400:          # <meta refresh> splash / redirect page
                    target = _norm(urljoin(final, pg.refresh))
                    if target not in queued and urlsplit(target).netloc == host:
                        queued.add(target)
                        queue.appendleft((target, depth, True))
                        first = False
                        continue
                if _js_only(pg.text, pg.scripts):
                    if first:
                        raise LinkError("This page is built by JavaScript in the browser, so there's no text for "
                                        "RetroRAG to copy. Try another page of the site, or save the page from your "
                                        "browser and add the file.")
                    log(f"  no text (built by JavaScript): {url}")
                else:
                    title = pg.title or None
                    if first and not link.title and pg.title:
                        lib.update_link(link.id, title=pg.title[:200])
                    data = r.text.encode("utf-8")
                    store.save_bytes(data, _file_name(final, r.ctype, True), _group_for(final) if site else "",
                                     final, title)
                    done += 1
                if site and depth < MAX_DEPTH:
                    base = urljoin(final, pg.base) if pg.base else final
                    for href in pg.links:
                        if href.startswith(("mailto:", "javascript:", "tel:", "#", "data:")):
                            continue
                        u = _norm(urljoin(base, href.strip()))
                        if u in queued or urlsplit(u).netloc != host or SKIP_URL.search(u):
                            continue
                        ext = _ext(u)
                        if ext in PAGE_EXT and u.startswith(prefix):
                            queued.add(u)
                            queue.append((u, depth + 1, True))
                        elif ext in FILE_EXT and link.include_files:
                            queued.add(u)
                            queue.append((u, depth + 1, False))
            else:
                if r.ctype.startswith("image/") or (not is_page_link and _ext(final) not in FILE_EXT
                                                    and r.ctype not in ("application/pdf", "text/plain")):
                    if first:
                        raise LinkError(f"The link points at a {r.ctype or 'file'} RetroRAG doesn't copy - "
                                        "download it and add it to the library instead.")
                    first = False
                    continue
                name = _file_name(final, r.ctype, False)
                store.save_bytes(r.content, name, _group_for(final) if site else "", final, None)
                if first and not link.title:
                    lib.update_link(link.id, title=unquote(name))
                done += 1
            first = False
        if progress:
            progress(limit, limit, "done")
        left = len(queue)
    what = f"Copied {done} page{'s' if done != 1 else ''}" + ("" if not site or not left else
                                                               f" (page limit reached; {left} more not copied)")
    return store.summary(what)


# ======================================================================== GitHub repositories
def _copy_repo(lib: Library, link: Link, store: _Store, progress, log) -> str:
    from . import ingest
    gh = github_repo(link.url)
    if not gh:
        raise LinkError("That isn't a GitHub repository address (https://github.com/<owner>/<repository>)")
    owner, repo = gh["owner"], gh["repo"]
    ref = link.branch.strip() or gh["ref"]
    sub = gh["path"]
    api = f"https://api.github.com/repos/{owner}/{repo}"
    js = {"Accept": "application/vnd.github+json"}
    with _client() as cl:
        r = fetch(cl, api, limit=2 << 20, headers=js)
        if r.status == 404:
            raise Gone("The repository isn't on GitHub any more (deleted, renamed or made private)")
        if r.status in (403, 429):
            raise Unreachable("GitHub's limit for anonymous requests was reached - try again in an hour")
        if r.status >= 400:
            raise Unreachable(f"GitHub answered {r.status}")
        import json
        info = json.loads(r.content)
        ref = ref or info.get("default_branch") or "HEAD"
        r = fetch(cl, f"{api}/commits/{quote(ref, safe='')}", limit=1 << 16,
                  headers={"Accept": "application/vnd.github.sha"})
        if r.status in (404, 422):
            raise LinkError(f"The repository has no branch, tag or commit called '{ref}'")
        if r.status >= 400:
            raise Unreachable(f"GitHub answered {r.status}")
        sha = r.content.decode("ascii", "replace").strip()
        if sha == link.version and lib.link_doc_ids(link.id):
            return f"No changes since the last copy (commit {sha[:10]})"
        if not link.title:
            lib.update_link(link.id, title=f"{owner}/{repo}" + (f"/{sub}" if sub else ""))
        if progress:
            progress(0, 0, f"downloading {owner}/{repo} @ {ref}")
        z = fetch(cl, f"https://codeload.github.com/{owner}/{repo}/zip/{sha}", limit=MAX_REPO)
        if z.status >= 400:
            raise Unreachable(f"GitHub didn't send the repository ({z.status})")
    out = store.tmp / "repo"
    with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
        members = [m for m in zf.infolist() if not m.is_dir()]
        if len(members) > MAX_REPO_FILES:
            raise LinkError(f"The repository has too many files ({len(members)}) - link a folder of it instead "
                            "(https://github.com/<owner>/<repo>/tree/<branch>/<folder>)")
        root = out.resolve()
        for m in members:
            target = (out / m.filename).resolve()
            if root not in target.parents:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(m) as fsrc, open(target, "wb") as fdst:
                shutil.copyfileobj(fsrc, fdst)
    tops = [p for p in out.iterdir() if p.is_dir()]
    base = tops[0] if len(tops) == 1 else out
    if sub:
        base = base / sub
        if not base.is_dir():
            raise LinkError(f"The repository has no folder '{sub}'")
    files = ingest.iter_folder(base)
    names = {f.as_posix().lower() for f in files}
    files = [f for f in files if not (f.suffix.lower() == ".md" and f.as_posix().lower()[:-3] in names)]
    group_root = repo + (f"/{sub}" if sub else "")
    for i, f in enumerate(files):
        if progress and i % 10 == 0:
            progress(i, len(files), f.name)
        rel = f.relative_to(base)
        parent = rel.parent.as_posix()
        group = group_root if parent == "." else f"{group_root}/{parent}"
        url = f"https://github.com/{owner}/{repo}/blob/{quote(ref, safe='')}/" + \
              quote(((sub + "/") if sub else "") + rel.as_posix())
        try:
            store.save_file(f, group, url, None)
        except Exception as e:
            log(f"  couldn't add {rel}: {e}")
    store.remove_missing()
    lib.update_link(link.id, version=sha)
    return store.summary(f"Copied {owner}/{repo} at {ref} (commit {sha[:10]})")


# ======================================================================== saved links
def _check(lib: Library, link: Link) -> str:
    from .htmltext import parse_html
    url = raw_github(link.url)
    with _client() as cl:
        r = fetch(cl, url, body=False)
        if r.status in (404, 410):
            raise Gone(f"The page isn't there any more ({r.status})")
        if r.status >= 500:
            raise Unreachable(f"{urlsplit(url).netloc} is having problems ({r.status})")
        if not link.title and r.status < 400:
            try:
                p = fetch(cl, url, limit=1 << 20)
                if p.ctype in PAGE_TYPES:
                    t = parse_html(p.text).title
                    if t:
                        lib.update_link(link.id, title=t[:200])
            except LinkError:
                pass
    return "Working" + (f" (the site answered {r.status}: it may need signing in)" if r.status in (401, 403) else "")


# ======================================================================== running a link
def run_link(lib: Library, link_id: int, progress=None, log=print) -> dict:
    """Copy (or re-copy) a link, or check a saved one. Records the outcome on the link -> summary dict."""
    link = lib.link(link_id)
    if not link:
        return {"link_id": link_id, "status": "error", "message": "No such link"}
    now = now_iso()
    store = _Store(lib, link, log) if link.copies else None
    try:
        if link.mode == "repo":
            msg = _copy_repo(lib, link, store, progress, log)
        elif link.mode in ("page", "site"):
            msg = _copy_pages(lib, link, store, link.mode == "site", progress, log)
        else:
            msg = _check(lib, link)
    except Gone as e:
        msg = str(e).rstrip(".") + "."
        upd = {"status": "gone", "status_msg": msg + (" The copy is kept in the library." if link.copies and
                                                      lib.link_doc_ids(link.id) else "")}
    except Refused as e:
        upd = {"status": "refused", "status_msg": str(e)}
    except Unreachable as e:
        fails = (link.fail_count or 0) + 1
        first = link.first_failed or now
        since = parse_iso(first)
        days = (datetime.now(timezone.utc) - since).days if since else 0
        if fails >= 2 and days >= GONE_AFTER_DAYS:
            upd = {"status": "gone", "status_msg": f"{e} - unreachable for {days} days, so marked as gone."}
        else:
            upd = {"status": "unreachable", "status_msg": f"{e}. RetroRAG will suggest trying again tomorrow."}
        upd.update(fail_count=fails, first_failed=first)
    except LinkError as e:
        upd = {"status": "error", "status_msg": str(e)}
    except Exception as e:                        # a bug or an odd file: report it, keep the app running
        upd = {"status": "error", "status_msg": f"Copy failed: {e}"}
    else:
        upd = {"status": "ok", "status_msg": msg, "last_fetched": now, "fail_count": 0, "first_failed": None}
    finally:
        if store:
            store.close()
    upd["last_checked"] = now
    lib.update_link(link_id, **upd)
    if store and (store.counts["added"] or store.counts["updated"] or store.counts["removed"]):
        lib.bump_index_version()
    out = {"link_id": link_id, "title": (lib.link(link_id) or link).label, "status": upd["status"],
           "message": upd["status_msg"]}
    if store:
        out.update(store.counts)
    log(f"{out['title']}: {out['message']}")
    return out


def run_links(lib: Library, link_ids: list[int], progress=None, log=print) -> list[dict]:
    out = []
    for k, lid in enumerate(link_ids):
        prefix = f"[{k + 1}/{len(link_ids)}] " if len(link_ids) > 1 else ""
        prog = (lambda i, n, m="", p=prefix: progress(i, n, p + m)) if progress else None
        out.append(run_link(lib, lid, progress=prog, log=log))
    return out


# ======================================================================== words for the UI and the AI
STATUS_WORDS = {"": "not done yet", "ok": "working", "unreachable": "couldn't reach", "gone": "gone",
                "refused": "site doesn't allow copying", "error": "needs attention"}


def describe_link(lib: Library, l: Link) -> dict:
    due = l.next_due()
    return {"link_id": l.id, "title": l.title, "url": l.url, "note": l.note, "kept_as": l.mode,
            "status": STATUS_WORDS.get(l.status, l.status), "status_detail": l.status_msg,
            "copied_documents": len(lib.link_doc_ids(l.id)) if l.copies else 0,
            "last_worked": (l.last_fetched or "")[:10] or None,
            "next_check": due.date().isoformat() if due else None}


def refresh_label(days: int) -> str:
    for d, label in REFRESH_CHOICES:
        if d == days:
            return "never" if d == 0 else f"every {label}"
    return f"every {days} days"
