"""Read a Project Gutenberg HTML edition into blocks for drip/cut.py. Standard library only.

The HTML keeps what the plain text loses: real headings with their levels, set-off examples
and quotations, verse, italics and the printed page numbers. blocks(html) returns a list of
(kind, html) pairs, the same shape cut.paragraphs() makes from plain text:

  h1 .. h4   a heading (h5 and h6 count as h4)
  p          a paragraph
  quote      a block quotation, letter or worked example, set off from the text
  verse      lines of poetry; line breaks are kept as "\n"
  note       a footnote
  pg         a printed page number (its text is the number); no words, never shown

The inline html keeps only <em>, <strong>, <sup> and <sub>, so it's safe to put in an email.
Each cell of a table is set off like a quotation (Strunk prints his wrong-and-right examples
in two columns). Gutenberg's header and licence, tables of contents, illustrations and their
captions, and transcriber's notes are left out.
"""
import html
import re
from html.parser import HTMLParser

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
BLOCK = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre", "dt", "dd", "section",
         "article", "header", "footer", "ul", "ol", "dl", "figure", "main", "body", "aside", "center", "hr",
         "table", "tr", "td", "th", "caption"}
SKIP_TAGS = {"head", "script", "style", "figure", "figcaption", "nav", "svg", "math", "button", "form"}
PAGE_CELL = re.compile(r"^[\divxlcIVXLC.,\s-]+$")
SKIP_CLASS = re.compile(r"\b(pg-boilerplate|toc|contents|caption|figcenter|figleft|figright|fig|illustration|"
                        r"transnote|tnote|hidden|hide|index)\b", re.I)
SKIP_ID = re.compile(r"^(pg-header|pg-footer|pg-machine-header|pg-start-separator|pg-end-separator)", re.I)
PAGENUM = re.compile(r"\b(pagenum|pageno|page-num|pgnum)\b", re.I)
QUOTE_CLASS = re.compile(r"\b(blockquote|blockquot|bq|quote|quotation|example|extract|letter|epigraph|citation|"
                         r"inscription|sign|telegram|notice)\b", re.I)
VERSE_CLASS = re.compile(r"\b(poem|poetry|stanza|verse|song|lg)\b", re.I)
STANZA_CLASS = re.compile(r"\b(stanza|lg)\b", re.I)
NOTE_CLASS = re.compile(r"\b(footnotes?|endnotes?|fnote)\b", re.I)
INLINE = {"i": "em", "em": "em", "cite": "em", "b": "strong", "strong": "strong", "sup": "sup", "sub": "sub"}
PAGE_NUMBER = re.compile(r"([0-9]+|[ivxlcdm]+)\s*$", re.I)


def page_number(attrs, text=""):
    """The printed page number in a pagenum element: its title, its id (page_12) or its text ({12})."""
    for value in (attrs.get("title", ""), re.sub(r"^(page|pg|p)[_-]?", "", attrs.get("id", ""), flags=re.I),
                  text.strip(" {}[]()").replace("Pg", "").replace("Page", "")):
        m = PAGE_NUMBER.search(value.strip())
        if value.strip() and m:
            return m.group(1)
    return ""


class Reader(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.stack = []        # open elements: (tag, flags)
        self.skip = 0          # > 0 inside something we leave out
        self.buf = []          # html of the block being read
        self.link_chars = 0    # characters inside internal links, to spot contents lists
        self.page = None       # a pagenum element being read: [attrs, text]
        self.tables = []       # open tables: where their blocks start, rows, rows that look like contents
        self.row = None        # the table row being read: did it link inside the book, its last cell

    # Context from the open elements
    def inside(self, flag):
        return any(flag in f for _, f in self.stack)

    def kind(self):
        for tag, flags in reversed(self.stack):
            if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                return "h" + str(min(int(tag[1]), 4))
        if self.inside("verse") or self.inside("pre"):
            return "verse"
        if self.inside("note"):
            return "note"
        if self.inside("quote") or self.inside("table"):
            return "quote"
        return "p"

    def flush(self):
        raw = "".join(self.buf)
        self.buf = []
        links, self.link_chars = self.link_chars, 0
        kind = self.kind()
        if kind == "verse":
            lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in raw.split("\n")]
            text = "\n".join(ln for ln in lines if ln)
        else:
            text = re.sub(r"\s+", " ", raw.replace("\n", " ")).strip()
        visible = re.sub(r"<[^>]+>", "", text)
        if self.row is not None and visible.strip():
            self.row["link"] = self.row["link"] or bool(links)
            self.row["last"] = visible.strip()
        if not visible.strip():
            return
        if links and links >= 0.6 * len(visible) and len(visible.split()) < 40:
            return  # a line of a contents list or index
        if kind.startswith("h") and len(visible.split()) > 40:
            kind = "p"  # an whole paragraph set in a heading tag
        self.out.append((kind, close_tags(text)))

    # Parser callbacks
    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        cls, ident = a.get("class", ""), a.get("id", "")
        if self.page is not None:
            return
        if tag in VOID:
            if tag == "br":
                if not self.skip:
                    self.buf.append("\n" if self.kind() == "verse" else " ")
            elif tag == "img" and not self.skip:
                alt = a.get("alt", "").strip()
                if 0 < len(alt) <= 2 and alt.isalpha():  # an illustrated drop cap
                    self.buf.append(html.escape(alt))
            elif tag == "hr" and not self.skip:
                self.flush()
            return
        if not self.skip and PAGENUM.search(cls):
            self.page = [a, ""]
            self.stack.append((tag, {"page"}))
            return
        flags = set()
        if self.skip or tag in SKIP_TAGS or SKIP_CLASS.search(cls) or SKIP_ID.match(ident) \
                or "display:none" in a.get("style", "").replace(" ", ""):
            flags.add("skip")
            self.skip += 1
        if tag == "blockquote" or QUOTE_CLASS.search(cls):
            flags.add("quote")
        if VERSE_CLASS.search(cls):
            flags.add("verse")
        if STANZA_CLASS.search(cls):
            flags.add("stanza")
        if tag == "pre":
            flags.add("pre")
        if NOTE_CLASS.search(cls):
            flags.add("note")
        if tag == "a" and a.get("href", "").startswith("#") and "fnanchor" not in cls:
            flags.add("link")
        if not self.skip and tag == "table":
            flags.add("table")
        if not self.skip:
            if tag in BLOCK:
                # Inside a poem each line is often a block of its own: keep it as a line of the stanza.
                if self.kind() == "verse" and "stanza" not in flags:
                    self.buf.append("\n")
                else:
                    self.flush()
                if tag == "table":
                    self.tables.append({"start": len(self.out), "rows": 0, "contents": 0})
                elif tag == "tr":
                    self.row = {"link": False, "last": ""}
            elif tag in INLINE:
                self.buf.append(f"<{INLINE[tag]}>")
        self.stack.append((tag, flags))

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        # Close up to the matching open tag; HTML sometimes leaves <p> open.
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                break
        else:
            return
        while len(self.stack) > i:
            t, flags = self.stack.pop()
            if "page" in flags and self.page is not None:
                attrs, text = self.page
                self.page = None
                number = page_number(attrs, text)
                if number and not self.skip:
                    self.flush_page(number)
                continue
            if self.skip:
                if "skip" in flags:
                    self.skip -= 1
                continue
            if t in BLOCK:
                if self.kind() == "verse" and "stanza" not in flags:
                    self.buf.append("\n")
                else:
                    self.flush_with(t, flags)
                if t == "tr" and self.row is not None and self.tables:
                    row, self.row = self.row, None
                    self.tables[-1]["rows"] += 1
                    if row["link"] or PAGE_CELL.match(row["last"] or "x"):
                        self.tables[-1]["contents"] += 1
                elif t == "table" and self.tables:
                    table = self.tables.pop()
                    if table["rows"] and table["contents"] >= 0.5 * table["rows"]:
                        del self.out[table["start"]:]  # a table of contents: page numbers or links
            elif t in INLINE:
                self.buf.append(f"</{INLINE[t]}>")

    def flush_with(self, tag, flags):
        # The block's kind comes from the element being closed, which is already off the stack.
        self.stack.append((tag, flags))
        self.flush()
        self.stack.pop()

    def flush_page(self, number):
        """A page number goes between blocks; one in mid-paragraph waits for the paragraph to end."""
        if "".join(self.buf).strip():
            self.buf.append(f"\x00{number}\x00")
        else:
            self.out.append(("pg", number))

    def handle_data(self, data):
        if self.page is not None:
            self.page[1] += data
            return
        if self.skip:
            return
        text = html.escape(data, quote=False)
        if self.inside("link"):
            self.link_chars += len(data.strip())
        self.buf.append(text if self.kind() == "verse" else text.replace("\n", " "))


def close_tags(text):
    """Drop inline tags left unbalanced by sloppy markup."""
    for tag in ("em", "strong", "sup", "sub"):
        opens, closes = text.count(f"<{tag}>"), text.count(f"</{tag}>")
        if opens > closes:
            text += f"</{tag}>" * (opens - closes)
        elif closes > opens:
            text = f"<{tag}>" * (closes - opens) + text
    return text.replace(f"<em></em>", "").replace("<strong></strong>", "")


DROP_CAP = re.compile(r"^((?:<[^>]+>)*)([A-Z])([A-Z]+)(?=[\s’',;:]+[a-z])")


def soften_drop_cap(text):
    """'IT is a truth' -> 'It is a truth': a drop cap and the capitals printed after it."""
    return DROP_CAP.sub(lambda m: m.group(1) + m.group(2) + m.group(3).lower(), text)


def blocks(source):
    """The book's blocks, in order, with page numbers found mid-paragraph moved after it."""
    reader = Reader()
    reader.feed(source)
    reader.close()
    reader.flush()
    out = []
    for kind, text in reader.out:
        if kind == "p":
            text = soften_drop_cap(text)
        if "\x00" in text:
            pages = re.findall(r"\x00([^\x00]*)\x00", text)
            text = re.sub(r"\x00[^\x00]*\x00", "", text)
            text = re.sub(r"  +", " ", re.sub(r" +\n|\n +", "\n", text)).strip()
            out.append((kind, text))
            out.extend(("pg", p) for p in pages)
        else:
            out.append((kind, text))
    return out
