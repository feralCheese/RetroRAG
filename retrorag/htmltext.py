"""HTML -> readable Markdown-ish text, plus the page's title and links (for web copies and local .html files).

Keeps what matters for reference material: headings, paragraphs, lists, tables and <pre> listings (memory maps,
code - whitespace kept exactly). Drops scripts, styles, forms and, where the page marks them, navigation menus,
headers, footers and sidebars. When a page says where its main content is (<main>, <article>, role=main, or the
usual ids/classes of wikis, blogs and GitHub), only that part is kept.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

SKIP = {"script", "style", "noscript", "template", "svg", "canvas", "iframe", "object", "embed", "form", "button",
        "select", "option", "textarea", "head", "title", "dialog", "map"}
CHROME = {"nav", "header", "footer", "aside", "menu"}        # page furniture: dropped
CHROME_HINT = re.compile(r"(?i)(^|[\s_-])(nav|navbar|navigation|menu|sidebar|breadcrumbs?|footer|header|masthead|"
                         r"cookie|banner|toc|skip|social|share|related-posts|comments?|mw-navigation|"
                         r"mw-panel|printfooter|catlinks|mw-jump|mw-jump-link|editsection|mw-editsection|sitesub|"
                         r"contentsub|jump-to-nav|noprint|mw-indicators|navbox|mw-cite-backlink)([\s_-]|$)")
MAIN_IDS = ("mw-content-text", "bodycontent", "main-content", "maincontent", "content", "main", "article",
            "page-content", "readme", "post", "primary")
MAIN_CLASSES = re.compile(r"(?i)(^|\s)(markdown-body|mw-parser-output|entry-content|post-content|article-body|"
                          r"article-content|page-content|main-content|content-body|documentation|rst-content|"
                          r"theme-doc-markdown|md-content)(\s|$)")
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track",
        "wbr", "keygen", "frame"}
BLOCK = {"p", "div", "section", "article", "main", "ul", "ol", "dl", "dt", "dd", "table", "blockquote", "figure",
         "figcaption", "address", "center", "body", "html", "fieldset", "details", "summary", "caption", "tbody",
         "thead", "tfoot", "frameset", "noframes"}


@dataclass
class Page:
    title: str = ""
    text: str = ""
    links: list[str] = field(default_factory=list)      # href / frame src values, as written
    base: str = ""                                     # <base href>
    scripts: int = 0
    refresh: str = ""                                  # <meta http-equiv=refresh> target


class _Events(HTMLParser):
    """Flatten the document into (kind, tag, attrs | text) events, collecting title / links on the way."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ev: list = []
        self.page = Page()
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "a" and a.get("href"):
            self.page.links.append(a["href"])
        elif tag in ("frame", "iframe") and a.get("src"):
            self.page.links.append(a["src"])
        elif tag == "base" and a.get("href") and not self.page.base:
            self.page.base = a["href"]
        elif tag == "script":
            self.page.scripts += 1
        elif tag == "meta" and a.get("http-equiv", "").lower() == "refresh":
            m = re.search(r"(?i)url\s*=\s*['\"]?([^'\";]+)", a.get("content", ""))
            if m:
                self.page.refresh = m.group(1).strip()
        self.ev.append(("start", tag, a))
        if tag in VOID:
            self.ev.append(("end", tag, None))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.ev.append(("end", tag, None))

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag not in VOID:
            self.ev.append(("end", tag, None))

    def handle_data(self, data):
        if self._in_title:
            self.page.title += data
        self.ev.append(("text", "", data))


def _is_chrome(tag: str, a: dict) -> bool:
    if tag in CHROME:
        return True
    if a.get("role") in ("navigation", "banner", "contentinfo", "complementary", "search"):
        return True
    if a.get("aria-hidden") == "true" or "hidden" in a:
        return True
    hint = f"{a.get('id', '')} {a.get('class', '')}"
    return bool(hint.strip()) and bool(CHROME_HINT.search(hint)) and tag in ("div", "section", "ul", "table", "td",
                                                                             "span", "p", "ol", "dl", "a", "li")


def _main_score(tag: str, a: dict) -> int:
    """How sure we are this element holds the page's main content (0 = not)."""
    ident = a.get("id", "").lower()
    if ident in ("mw-content-text", "readme"):
        return 6
    if ident == "bodycontent":
        return 5
    if MAIN_CLASSES.search(a.get("class", "")):
        return 4
    if tag == "main" or a.get("role") == "main":
        return 3
    if tag == "article":
        return 2
    if ident in MAIN_IDS:
        return 1
    return 0


class _Writer:
    def __init__(self):
        self.out: list[str] = []
        self.pre = 0
        self.lists: list[list] = []          # [kind, counter]
        self.row: list[str] | None = None
        self.cell: list[str] | None = None
        self.table_rows: list[tuple[list[str], bool]] | None = None
        self.row_is_head = False

    # text sinks: a table cell collects its own text
    def _sink(self) -> list[str]:
        return self.cell if self.cell is not None else self.out

    def text(self, t: str):
        if self.pre:
            self._sink().append(t)
            return
        t = re.sub(r"\s+", " ", t)
        sink = self._sink()
        if not sink or sink[-1].endswith((" ", "\n")):
            t = t.lstrip()
        if t:
            sink.append(t)

    def block(self, n: int = 2):
        if self.cell is not None:
            self.cell.append(" ")
            return
        tail = "".join(self.out[-3:])
        have = len(tail) - len(tail.rstrip("\n")) if tail.strip() or self.out else n
        if not self.out:
            return
        if have < n:
            # strip trailing spaces before breaking
            while self.out and self.out[-1].endswith(" ") and not self.pre:
                self.out[-1] = self.out[-1].rstrip(" ")
                if not self.out[-1]:
                    self.out.pop()
            self.out.append("\n" * (n - have))

    def start(self, tag: str, a: dict):
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.block()
            if self.cell is None:
                self.out.append("#" * int(tag[1]) + " ")
        elif tag == "pre":
            self.block()
            if self.cell is None:
                self.out.append("```\n")
            self.pre += 1
        elif tag == "br":
            self._sink().append("\n" if self.cell is None else " ")
        elif tag == "hr":
            self.block()
            self.out.append("---")
            self.block()
        elif tag in ("ul", "ol"):
            self.block(1 if self.lists else 2)
            self.lists.append([tag, 0])
        elif tag == "li":
            self.block(1)
            depth = max(0, len(self.lists) - 1)
            if self.lists and self.lists[-1][0] == "ol":
                self.lists[-1][1] += 1
                bullet = f"{self.lists[-1][1]}. "
            else:
                bullet = "- "
            if self.cell is None:
                self.out.append("  " * depth + bullet)
        elif tag == "dt":
            self.block(1)
        elif tag == "dd":
            self.block(1)
            if self.cell is None:
                self.out.append(": ")
        elif tag == "table":
            self.block()
            self.table_rows = []
        elif tag == "tr":
            self.row = []
            self.row_is_head = False
        elif tag in ("td", "th"):
            if self.row is None:
                self.row = []
            self.cell = []
            if tag == "th":
                self.row_is_head = True
        elif tag == "blockquote":
            self.block()
        elif tag in BLOCK or tag == "p":
            self.block()

    def end(self, tag: str):
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.block()
        elif tag == "pre":
            self.pre = max(0, self.pre - 1)
            if self.cell is None:
                if self.out and not self.out[-1].endswith("\n"):
                    self.out.append("\n")
                self.out.append("```")
            self.block()
        elif tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
            self.block(1 if self.lists else 2)
        elif tag in ("td", "th"):
            if self.cell is not None and self.row is not None:
                self.row.append(re.sub(r"\s+", " ", "".join(self.cell)).strip().replace("|", "\\|"))
            self.cell = None
        elif tag == "tr":
            if self.cell is not None:
                self.end("td")
            if self.row is not None and any(self.row) and self.table_rows is not None:
                self.table_rows.append((self.row, self.row_is_head))
            elif self.row is not None and any(self.row):
                self.out.append(" | ".join(self.row))
                self.block(1)
            self.row = None
        elif tag == "table":
            if self.row is not None:
                self.end("tr")
            rows = self.table_rows or []
            self.table_rows = None
            if rows:
                self._emit_table(rows)
            self.block()
        elif tag in BLOCK or tag in ("p", "li", "dt", "dd", "blockquote"):
            self.block(1 if tag in ("li", "dt", "dd") else 2)

    def _emit_table(self, rows):
        width = max(len(r) for r, _ in rows)
        if width == 1:                        # layout table holding one column of content
            for r, _ in rows:
                self.out.append(r[0])
                self.block()
            return
        lines = []
        for i, (r, head) in enumerate(rows):
            cells = r + [""] * (width - len(r))
            lines.append("| " + " | ".join(cells) + " |")
            if i == 0:
                lines.append("|" + "|".join([" --- "] * width) + "|")
        self.block()
        self.out.append("\n".join(lines))
        self.block()

    def result(self) -> str:
        s = "".join(self.out)
        s = re.sub(r"[ \t]+\n", "\n", s)
        s = re.sub(r"\n{3,}", "\n\n", s)
        return s.strip() + "\n" if s.strip() else ""


def _render(events: list, start: int = 0, stop: int | None = None, drop_chrome: bool = True) -> str:
    w = _Writer()
    skip = 0            # depth inside a skipped element
    skip_tag: list[str] = []
    for kind, tag, val in events[start:stop]:
        if kind == "start":
            if skip:
                if tag == skip_tag[-1] and tag not in VOID:
                    skip += 1
                continue
            if tag in SKIP or (drop_chrome and _is_chrome(tag, val)):
                if tag not in VOID:
                    skip = 1
                    skip_tag.append(tag)
                continue
            if tag == "img":
                alt = val.get("alt", "").strip()
                if len(alt) > 3 and not re.search(r"\.(gif|png|jpe?g|svg)$", alt, re.I):
                    w.text(f" [image: {alt}] ")
                continue
            w.start(tag, val)
        elif kind == "end":
            if skip:
                if tag == skip_tag[-1]:
                    skip -= 1
                    if skip == 0:
                        skip_tag.pop()
                continue
            w.end(tag)
        else:
            if not skip:
                w.text(val)
    return w.result()


def _main_range(events: list) -> tuple[int, int] | None:
    """Events of the element most likely to hold the main content."""
    best, best_i = 0, -1
    for i, (kind, tag, val) in enumerate(events):
        if kind == "start":
            sc = _main_score(tag, val)
            if sc > best:
                best, best_i = sc, i
    if best_i < 0:
        return None
    tag = events[best_i][1]
    depth = 0
    for j in range(best_i, len(events)):
        kind, t, _ = events[j]
        if t != tag:
            continue
        if kind == "start":
            depth += 1
        elif kind == "end":
            depth -= 1
            if depth == 0:
                return best_i, j + 1
    return best_i, len(events)


def parse_html(src: str) -> Page:
    p = _Events()
    try:
        p.feed(src)
        p.close()
    except Exception:              # html.parser is forgiving; never let one odd page stop a copy
        pass
    page = p.page
    page.title = re.sub(r"\s+", " ", page.title).strip()
    whole = _render(p.ev)
    rng = _main_range(p.ev)
    text = whole
    if rng:
        main = _render(p.ev, *rng)
        if len(main) >= 200 or len(main) >= 0.3 * len(whole):
            text = main
    if len(text.strip()) < 40:               # a page made of "chrome" only (old frame pages, menus): keep it all
        text = _render(p.ev, drop_chrome=False)
    if page.title and not re.match(r"#\s", text):
        h1 = re.search(r"(?m)^# (.+)$", text)
        if not h1:
            text = f"# {page.title}\n\n" + text
    page.text = text
    return page


def html_to_markdown(src: str) -> str:
    return parse_html(src).text
