"""
mysolution: a knowledge system over dated website snapshots (Dupaco hackathon).

Pipeline
  ingest   every page of every snapshot is kept. Each unique page version is parsed once into
           rate/fee facts (markdown tables + "label: value" lines), sentences (for search), links,
           branch-hours blocks, the branch locator, routing numbers and dated claims.
           A snapshot's *view* = pages present in it + the newest copy of pages seen earlier
           (carried forward, ranked lower).
  answer   picks the snapshot for `as_of` (or a date written in the question), then routes:
           routing number / link / hours / branch address / rate+fee facts / sentence search,
           with an honest refusal (confident=False, no digits) when nothing supports an answer.
  conflicts  stale copies of a fact on lower-authority pages, values that changed between
           snapshots, and contradictory dated claims (branch counts, award counts).

Deterministic, stdlib only, no API key. Heavy work happens at ingest; answers are cached.
"""

import hashlib
import math
import re
import sys
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

sys.path.insert(0, str(next(d / "harness" for d in Path(__file__).resolve().parents if (d / "harness" / "contract.py").exists())))
from contract import Answer, Conflict, Snapshot, Source  # noqa: E402


# =============================================================================================
# URLs, authority, dates
# =============================================================================================

def parse_url(url: str):
    return urlparse(url if "//" in url else "https://" + url)


def url_path(url: str) -> str:
    return parse_url(url).path.rstrip("/").lower()


def canon_url(url: str) -> str:
    p = parse_url(url)
    return f"{p.scheme}://{p.netloc.lower()}{p.path.rstrip('/')}"


def authority(url: str) -> int:
    """Trust in a page as a source of *current* facts.
    product/rate page > rate sheet > general > promo > calculator > blog/newsroom > legacy host."""
    u = parse_url(url)
    host, p = u.netloc.lower(), u.path.rstrip("/").lower()
    if host.startswith("info."):
        return 5                                                   # legacy landing pages
    if p.startswith(("/newsroom/tag", "/newsroom/author", "/business/resources/tag", "/learn/blog/tag")):
        return 0                                                   # index/listing pages: not indexed
    if p.startswith(("/learn", "/newsroom", "/business/resources", "/blog")) or re.match(r"^/\d{4}/", p):
        return 10
    if "calculator" in p:
        return 30
    if p.endswith("-lp") or p.startswith(("/switch-and-earn", "/earn-more", "/promo")):
        return 40
    if p.startswith(("/personal/", "/business/")) or p == "/checking":
        return 100
    if p in ("/rates", "/business-rates") or p.startswith("/rates/"):
        return 95
    if p.startswith(("/about", "/contact")) or p == "":
        return 80
    return 50


MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}
MONTH_RX = "(january|february|march|april|may|june|july|august|september|october|november|december)"
DATE_LONG = re.compile(MONTH_RX + r"\s+(\d{1,2}),\s*(\d{4})", re.I)
DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
DATE_US = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")


def iso(y, m, d) -> str:
    return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"


def pub_date(md: str) -> str:
    """Publication date of an article: the first 'Month D, YYYY' in its first lines ('' if none)."""
    m = DATE_LONG.search("\n".join(md.split("\n")[:15]))
    return iso(m.group(3), MONTHS[m.group(1).lower()], m.group(2)) if m else ""


def month_end(y: int, m: int) -> str:
    return iso(y, m, [31, 29 if y % 4 == 0 else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])


QDATE_RX = re.compile(r"\b(?:as of|in|on|during|back in|around|from)\s+(?:the end of\s+)?" + MONTH_RX +
                      r"(?:\s+(\d{1,2}))?,?\s+(\d{4})\b", re.I)
QISO_RX = re.compile(r"\b(?:as of|on|in)\s+(\d{4}-\d{2}-\d{2})\b", re.I)


def question_date(q: str) -> Optional[str]:
    m = QISO_RX.search(q)
    if m:
        return m.group(1)
    m = QDATE_RX.search(q)
    if m:
        y, mo = int(m.group(3)), MONTHS[m.group(1).lower()]
        return iso(y, mo, m.group(2)) if m.group(2) else month_end(y, mo)
    return None


# =============================================================================================
# Text helpers
# =============================================================================================

TOKEN = re.compile(r"[a-z0-9]+")
NUMWORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
            "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "seventeen": 17, "eighteen": 18, "twenty": 20,
            "twenty-four": 24, "thirty": 30, "thirty-six": 36, "forty-eight": 48, "sixty": 60,
            "seventy-two": 72, "eighty-four": 84}
NUMWORD_RX = re.compile(r"\b(" + "|".join(sorted(NUMWORDS, key=len, reverse=True)) + r")[\s-]+(months?|years?|yrs?|mos?)\b", re.I)
ABBR = {"avenue": "ave", "street": "st", "road": "rd", "drive": "dr", "lane": "ln", "boulevard": "blvd",
        "south": "so", "north": "n", "east": "e", "west": "w", "highway": "hwy", "court": "ct", "place": "pl"}
CANON = {"closure": "close", "closing": "close", "closed": "close", "closes": "close", "opened": "open",
         "opening": "open", "cds": "certificate", "cd": "certificate", "certificates": "certificate",
         "eligible": "eligibility", "join": "join", "joining": "join", "membership": "member",
         "employees": "employee", "volunteering": "volunteer", "branches": "branch", "recognized": "recognition",
         "recognize": "recognition", "rankings": "ranking", "ranked": "ranking", "ranks": "ranking"}


def stem(t: str) -> str:
    t = CANON.get(t, t)
    for suf in ("ibility", "ability", "ility", "ible", "able"):   # flexibility/flexible -> flex
        if t.endswith(suf) and len(t) - len(suf) >= 4:
            return t[:-len(suf)]
    if len(t) > 3 and t.endswith("ies"):
        return t[:-3] + "y"
    if len(t) > 3 and t.endswith("s") and not t.endswith(("ss", "us", "is")):
        return t[:-1]
    return t


def tokens(text: str) -> list:
    return [stem(t) for t in TOKEN.findall(text.lower())]


def normalize_question(q: str) -> str:
    q = NUMWORD_RX.sub(lambda m: f"{NUMWORDS[m.group(1).lower()]}-{m.group(2)}", q)
    # credit-union vocabulary: a "share certificate" / "term share certificate" is a certificate (CD)
    q = re.sub(r"\b(?:term\s+)?share\s+certificates?\b", "certificate", q, flags=re.I)
    return re.sub(r"\s+", " ", q).strip()


TERM_RE = re.compile(r"(\d{1,3})\s*[- ]?\s*(months?|mos?|years?|yrs?)\b", re.I)
MONEY_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
YEAR_RANGE_RE = re.compile(r"\b((?:19|20)\d\d)\s*[-\u2013\u2014/]\s*(\d{2,4})\b")
PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
DOLLAR_VAL_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?")
EFFECTIVE_RE = re.compile(r"effective as of\s+(\d{1,2}/\d{1,2}/\d{4})", re.I)
EXPIRED_RE = re.compile(r"\b(?:ended|expired|ends|expires|valid (?:through|until))\s+(?:on\s+)?"
                        r"(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}|" + MONTH_RX + r"\s+\d{1,2},\s*\d{4})", re.I)


def any_date_iso(s: str) -> str:
    m = DATE_ISO.search(s)
    if m:
        return iso(*m.groups())
    m = DATE_US.search(s)
    if m:
        return iso(m.group(3), m.group(1), m.group(2))
    m = DATE_LONG.search(s)
    if m:
        return iso(m.group(3), MONTHS[m.group(1).lower()], m.group(2))
    return ""


def term_months(text: str):
    m = TERM_RE.search(text)
    if not m:
        return None
    n = int(m.group(1))
    return n * 12 if m.group(2).lower().startswith("y") else n


def money(s: str) -> float:
    return float(s.replace(",", ""))


def amount_range(text: str):
    """'$25,000+' -> (25000, inf); '$10,000 – $24,999.99' -> (10000, 24999.99); '$832,750 or less' -> (0, 832750)."""
    nums = [money(m.group(1)) for m in MONEY_RE.finditer(text)]
    if len(nums) == 1:
        m = re.search(r"\$\s?[\d,]+(?:\.\d+)?\s*[-\u2013]\s*([\d,]{4,}(?:\.\d+)?)", text)
        if m:
            nums.append(money(m.group(1)))
    if not nums:
        m = re.search(r"(\d{1,3}(?:,\d{3})+)\s*[-\u2013]\s*(\d{1,3}(?:,\d{3})+)", text)
        if not m:
            return None
        nums = [money(m.group(1)), money(m.group(2))]
    if len(nums) >= 2:
        return (min(nums[:2]), max(nums[:2]))
    if re.search(r"or less|and under|up to|or below", text, re.I):
        return (0.0, nums[0])
    return (nums[0], math.inf)


def model_year_range(text: str):
    m = YEAR_RANGE_RE.search(text)
    if m and re.search(r"model|year", text, re.I):
        a, b = int(m.group(1)), m.group(2)
        b = int(b) if len(b) == 4 else int(str(a)[:2] + b)
        return (min(a, b), max(a, b))
    m = re.search(r"\b((?:19|20)\d\d)\b.*(?:and older|or older)", text, re.I)
    if m:
        return (0, int(m.group(1)))
    return None


# =============================================================================================
# Facts (rates, fees)
# =============================================================================================

@dataclass
class Fact:
    product: str                 # caption / product name
    label: str                   # row label (product, tier, term...)
    measures: dict               # {"APY": "0.20%", "dividend rate": "0.20%", "fee": "$2.50", ...}
    url: str
    effective: str = ""
    authority: int = 50
    table_id: int = 0
    term: Optional[int] = None
    amount: Optional[tuple] = None
    model_years: Optional[tuple] = None
    my_rank: Optional[int] = None    # position of the model-year tier in its table (0 = newest)
    core: set = field(default_factory=set)      # tokens from product + label (strong)
    context: set = field(default_factory=set)   # tokens from section, page title, url (weak)
    snapshot_id: str = ""
    taken_on: str = ""
    present: bool = True             # False = carried forward from an older snapshot

    def main_measure(self) -> str:
        for k in ("APY", "APR", "rate", "dividend rate", "fee", "value"):
            if k in self.measures:
                return k
        return next(iter(self.measures))

    def primary_value(self) -> float:
        v = self.measures[self.main_measure()]
        m = PCT_RE.search(v) or re.search(r"(\d[\d,]*(?:\.\d+)?)", v)
        return float(m.group(1).replace(",", "")) if m else 0.0

    def key(self):
        return (self.product, self.label, tuple(sorted(self.measures.items())))


GENERIC_HEADERS = {"", "account type", "balance tier", "balance", "term", "certificate", "program", "loan program",
                   "product", "card type", "ira type", "loan type", "loan amount", "category", "account"}
TABLE_SEP = re.compile(r"^\|\s*:?-{3,}")


def measure_name(header: str):
    h = header.lower()
    if "min." in h or "max." in h or "minimum" in h or "payment" in h or "points" in h:
        return "skip"
    if "apy" in h or "annual percentage yield" in h:
        return "APY"
    if "apr" in h or "annual percentage rate" in h:
        return "APR"
    if "dividend" in h:
        return "dividend rate"
    if re.fullmatch(r"\W*(interest )?rate\W*", h):
        return "rate"
    if re.search(r"\bfee\b|amount charged|\bcost\b", h):
        return "fee"
    return None


def split_row(line: str) -> list:
    cells = line.strip().strip("|").split("|")
    return [re.sub(r"\\\*|\*", "", c).strip() for c in cells]


def is_caption(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 70 or s[0] in "|[!<\\" or s.startswith("Rates are"):
        return False
    if re.match(r"^\d", s) or "effective as of" in s.lower() or s.endswith((".", ":")):
        return False
    return True


def clean_value(cell: str, kind: str) -> str:
    if kind == "fee":
        m = DOLLAR_VAL_RE.search(cell)
        return m.group(0).replace(" ", "") if m else ""
    vals = PCT_RE.findall(cell)
    return "\u2013".join(f"{v}%" for v in vals[:2]) if vals else ""


# "label: value" facts must be short and start with the number ("2.95%", "$2.50 per card", "as low as 5.39% APR"),
# and the label must say what is measured. Prose like "Lower payments: 30-45% less than..." is not a fact.
LINE_VALUE_RE = re.compile(r"^(?:as low as\s+|only\s+)?\$?\s?\d[\d,]*(?:\.\d+)?\s*%?(?:\s*[-–]\s*\$?\d[\d,]*(?:\.\d+)?%?)?"
                           r"(?:\s*(?:apr|apy|fee|per \w+|each|a month|monthly))?\s*[.;]?\s*(?:\(.*\))?$", re.I)
LINE_LABEL_RE = re.compile(r"\b(apy|apr|rate|yield|dividend|fee|charge|cost|price|minimum|maximum)\b", re.I)
LINE_FACT_RE = re.compile(r"^\s*(?:[-*]\s+)?\**([A-Za-z0-9][^:|\n]{2,80}?)\**:\s*(.{1,160})$")


def extract_facts(md: str, url: str, title: str, auth: int) -> list:
    lines = md.split("\n")
    facts, tid = [], 0
    ctx_tokens = set(tokens(title)) | set(tokens(url_path(url).replace("-", " ")))
    i = 0
    while i < len(lines):
        line = lines[i]
        # ---- "label: value" lines (e.g. "6-month certificate APY: 2.95%", "Replacement fee: $2.50")
        if i + 1 >= len(lines) or not (line.startswith("|") and TABLE_SEP.match(lines[i + 1])):
            m = LINE_FACT_RE.match(line)
            if m and LINE_VALUE_RE.match(m.group(2).strip()) and LINE_LABEL_RE.search(m.group(1)):
                lab, val = m.group(1).strip(), m.group(2).strip()
                if not EXPIRED_RE.search(val):
                    low = (lab + " " + val).lower()
                    kind = ("APY" if "apy" in low or "yield" in low else "APR" if "apr" in low else
                            "fee" if not PCT_RE.search(val) else "rate")
                    v = clean_value(val, kind) if kind != "fee" else (DOLLAR_VAL_RE.search(val).group(0).replace(" ", ""))
                    if v:
                        desc = f"{lab} {re.sub(r'[$]?[0-9][0-9.,]*%?', ' ', val)}"
                        core = set(tokens(desc))
                        facts.append(Fact(product=lab, label="", measures={kind: v}, url=url, authority=auth,
                                          term=term_months(lab) or term_months(val), amount=None,
                                          core=core, context=ctx_tokens - core, table_id=-1))
            i += 1
            continue
        # ---- markdown tables
        header = split_row(line)
        j = i + 2
        rows = []
        while j < len(lines) and lines[j].startswith("|"):
            rows.append(split_row(lines[j]))
            j += 1
        tid += 1
        kinds = [measure_name(h) for h in header]
        if not any(k not in (None, "skip") for k in kinds) or not any(re.search(r"%|\$", "".join(r)) for r in rows):
            i = j
            continue
        caps = []
        for k in range(i - 1, max(-1, i - 14), -1):
            if is_caption(lines[k]):
                caps.append(lines[k].strip().lstrip("#").strip())
                if len(caps) == 2:
                    break
        caption = caps[0] if caps else ""
        section = caps[1] if len(caps) > 1 else ""
        if header and header[0].lower() not in GENERIC_HEADERS and kinds[0] is None:
            caption = f"{caption} {header[0]}".strip()
        eff = ""
        tail = "\n".join(lines[j:j + 8])
        m = EFFECTIVE_RE.search(tail)
        if m:
            eff = m.group(1)
        if EXPIRED_RE.search(tail) and not eff:
            i = j
            continue
        table_facts = []
        for r in rows:
            if len(r) != len(header):
                continue
            measures, label_parts = {}, []
            for kind, cell in zip(kinds, r):
                if kind == "skip":
                    continue
                if kind:
                    v = clean_value(cell, kind)
                    if v:
                        measures[kind] = v
                elif cell and cell not in ("\u2014", "-"):
                    label_parts.append(cell)
            if not measures:
                continue
            label = " | ".join(label_parts)
            core = set(tokens(caption)) | set(tokens(label))
            table_facts.append(Fact(
                product=caption, label=label, measures=measures, url=url, effective=eff, authority=auth,
                table_id=tid, term=term_months(label) or term_months(caption), amount=amount_range(label),
                model_years=model_year_range(label), core=core,
                context=(set(tokens(section)) | ctx_tokens) - core))
        ranges = sorted({f.model_years for f in table_facts if f.model_years}, key=lambda r: -r[1])
        for f in table_facts:
            if f.model_years:
                f.my_rank = ranges.index(f.model_years)
        facts.extend(table_facts)
        i = j
    return facts


# =============================================================================================
# Page documents: sentences, links, hours, branches, routing numbers
# =============================================================================================

LINK_MD = re.compile(r"(!?)\[([^\]]*)\]\((\S+?)(?:\s+\"[^\"]*\")?\)")
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"\u201c(])")
DAY_LINE = re.compile(r"^(?:\*\*[^*]+\*\*\s*)?((?:mon|tue|wed|thu|fri|sat|sun)[a-z]*(?:\s*[-\u2013]\s*[a-z]+)?)\s*:\s*(.+)$", re.I)
ADDRESS_RE = re.compile(r"^\d+[^,]{2,60},\s*[A-Za-z .]+,\s*[A-Z]{2},?\s*\d{5}")
ROUTING_RE = re.compile(r"routing (?:number|no\.?|#)?[^0-9\n]{0,40}(\d{9})\b", re.I)


@dataclass
class Sent:
    doc: int
    text: str
    toks: frozenset
    head: frozenset       # tokens of the enclosing heading + page title
    head_text: str = ""   # the enclosing heading itself (used to give a bare sentence its subject)
    title: frozenset = frozenset()


@dataclass
class Doc:
    url: str
    title: str
    authority: int
    pub: str
    snapshot_id: str
    taken_on: str
    facts: list = field(default_factory=list)
    links: list = field(default_factory=list)          # (anchor, url, context)
    hours: list = field(default_factory=list)          # (kind, schedule tuple, context text)
    branches: list = field(default_factory=list)       # (name, address, token set)
    routing: list = field(default_factory=list)
    sent_ids: list = field(default_factory=list)


def clean_inline(s: str, links: list = None) -> str:
    def _link(m):
        if m.group(1):
            return ""
        if links is not None and m.group(3).startswith("http"):
            links.append((m.group(2), m.group(3)))
        return m.group(2)
    s = LINK_MD.sub(_link, s)
    s = re.sub(r"\\([*_#>\[\]()-])", r"\1", s)
    s = re.sub(r"[*_]{2,}|^#+\s*|^>\s*|^[-*]\s+", "", s.strip())
    return s.replace("\\r\\n", " ").strip()


def parse_hours(lines: list) -> list:
    """**Lobby** / **Drive-up** blocks followed by 'Day(-Day): times' lines."""
    out = []
    for i, line in enumerate(lines):
        m = re.match(r"^\s*\*\*\s*(lobby|drive[- ]?up|drive[- ]?thru)\s*(?:hours)?\s*\*\*\s*(.*)$", line, re.I)
        if not m:
            continue
        kind = "lobby" if m.group(1).lower() == "lobby" else "drive-up"
        sched, rest = [], m.group(2).strip()
        cand = ([rest] if rest else []) + lines[i + 1:i + 14]
        for c in cand:
            c = c.strip()
            if not c:
                continue
            if sched and c.startswith("**"):     # next block (e.g. **Drive-up** after **Lobby**)
                break
            d = DAY_LINE.match(c)
            if not d:
                break
            sched.append(f"{d.group(1).strip()}: {d.group(2).strip()}")
        if sched:
            ctx = " ".join(x.strip() for x in lines[max(0, i - 12):i] if x.strip() and not x.startswith("!"))
            out.append((kind, tuple(sched), clean_inline(ctx[-600:])))
    return out


def parse_locator(lines: list) -> list:
    out = []
    for i, line in enumerate(lines):
        if ADDRESS_RE.match(line.strip()):
            names = [lines[k].strip().lstrip("#").strip() for k in range(max(0, i - 4), i) if lines[k].strip()]
            addr = line.strip()
            name = " / ".join(names[-2:])
            toks = {ABBR.get(t, t) for t in tokens(addr + " " + name)}
            out.append((name, addr, toks))
    return out


# =============================================================================================
# Question parsing
# =============================================================================================

SYNONYMS = {
    "mortgage": {"mortgage", "conventional"},
    "car": {"auto"}, "vehicle": {"auto"}, "automobile": {"auto"}, "truck": {"auto"},
    "heloc": {"heloc", "equity"}, "card": {"visa", "card"}, "credit": {"visa", "credit"},
    "open": {"open", "close"}, "close": {"close", "open"},
}
GENERIC = set("""
a an the is are was were be been what whats which who whom how much many do does did can could would will shall
should you your i im me my we our us it its of on in at to for from with and or about any all some this that these
those there their dupaco dupacos rate rate interest apy apr yield dividend annual percentage current currently right
now today tell give show provide list know like want please get earn pay paying offer account s on
balance between tier level amount type kind option available still also just so if as by up out than then here
need looking find see check say said thing way new go going has have having had receive received got gotten
january february march april may june july august september october november december
standard purchase regular ongoing base variable usual normal typical
""".split())
GENERIC = GENERIC | {stem(w) for w in GENERIC}      # questions are stemmed before this check ("does" -> "doe")
DISTINGUISH = {"business", "jumbo", "roth", "traditional", "ira", "refinance", "introductory", "intro",
               "transfer", "health", "mini", "max", "minimax", "summer", "special"}
SUPER_HI = {"highest", "best", "top", "maximum", "most"}
SUPER_LO = {"lowest", "cheapest", "minimum", "least"}

RATE_Q = re.compile(r"\b(rates?|apys?|aprs?|yields?|dividends?|interest)\b|%", re.I)
RATE_ASK = re.compile(r"\b(rates?|apys?|aprs?|yields?|dividend rates?|interest rates?)\b", re.I)
FEE_Q = re.compile(r"\b(fees?|cost|costs|charge|charges|how much is|how much does|price)\b", re.I)
MEASURE_Q = [("APY", re.compile(r"\bapy\b|annual percentage yield|\byield\b", re.I)),
             ("APR", re.compile(r"\bapr\b|annual percentage rate", re.I)),
             ("dividend rate", re.compile(r"dividend", re.I))]
MEASURE_ORDER = ["rate", "dividend rate", "APY", "APR", "fee", "value"]
LIST_Q = re.compile(r"\b(what|which)\b.*\b(do you|does dupaco) (offer|have)\b|\ball\b|"
                    r"\b(the|your) [a-z ]*(cards|loans|accounts|certificates)\b", re.I)
LINK_Q = re.compile(r"\b(link|url|web ?address|web ?page|website|which page|what page)\b|\bonline\b.*\b(join|apply|open)\b|"
                    r"\b(join|apply|open)\b.*\bonline\b", re.I)
HOURS_Q = re.compile(r"\bhours?\b|\bwhat time\b|\bopen until\b", re.I)
ADDRESS_Q = re.compile(r"\b(address|located|location of|where is)\b", re.I)
ROUTING_Q = re.compile(r"\brouting\b|\baba\b", re.I)
SAME_Q = re.compile(r"\b(same|all branches|every branch|differ|different|vary|varies)\b", re.I)
OFFER_Q = re.compile(r"\b(do|does) (you|dupaco) (have|offer|provide)\b|\bany\b[\w\s]{0,30}\boptions?\b", re.I)
BRANCH_NAME_RX = re.compile(r"\b([A-Z][\w.'&-]*(?:\s+[A-Z][\w.'&-]*){0,3})\s+(?i:branch|office|location)\b")
NOT_BRANCH = {"dupaco", "the", "your", "our", "all", "any", "each", "every", "nearest", "closest", "local", "main",
              "my", "a", "which", "what", "do", "does", "is", "are", "can", "general", "branch"}


@dataclass
class Query:
    content: list
    term: Optional[int] = None
    amount: Optional[float] = None
    year: Optional[int] = None
    measure: Optional[str] = None
    superlative: Optional[str] = None
    list_mode: bool = False
    fee: bool = False
    words: set = field(default_factory=set)


def parse_question(q: str) -> Query:
    toks = tokens(q)
    words = set(toks)
    content = [t for t in toks if t not in GENERIC and not t.isdigit() and len(t) > 1]
    qu = Query(content=list(dict.fromkeys(content)), words=words)
    qu.term = term_months(q)
    amts = [money(m.group(1)) for m in MONEY_RE.finditer(q)]
    qu.amount = amts[0] if amts else None
    ym = re.search(r"\b(19[89]\d|20[0-3]\d)\b", q)
    if ym and re.search(r"model|vehicle|car|auto|truck|motorcycle", q, re.I):
        qu.year = int(ym.group(1))
    for name, rx in MEASURE_Q:
        if rx.search(q):
            qu.measure = name
            break
    qu.list_mode = bool(LIST_Q.search(q))
    qu.fee = bool(FEE_Q.search(q)) and not RATE_ASK.search(q)
    if words & SUPER_HI:
        qu.superlative = "hi"
    elif words & SUPER_LO:
        qu.superlative = "lo"
    qu.content = [t for t in qu.content if t not in ("month", "year", "mo", "yr", "model", "fee", "cost", "charge",
                                                       "price", "much")]
    return qu


def token_hit(t: str, bag) -> bool:
    return t in bag or bool(SYNONYMS.get(t, set()) & bag)


def score_fact(qu: Query, f: Fact):
    """(score, coverage), or None if the fact contradicts a qualifier in the question."""
    if qu.term and f.term and qu.term != f.term:
        return None
    if qu.year and f.model_years and not (f.model_years[0] <= qu.year <= f.model_years[1]):
        return None
    if qu.amount is not None and f.amount and not (f.amount[0] <= qu.amount <= f.amount[1]):
        return None
    if qu.fee != ("fee" in f.measures):
        return None
    if not qu.content:
        return None
    hits, score = 0, 0.0
    for t in qu.content:
        if token_hit(t, f.core):
            hits += 1
            score += 1.0
        elif token_hit(t, f.context):
            hits += 1
            score += 0.5
    coverage = hits / len(qu.content)
    for w in DISTINGUISH:
        if w in f.core and w not in qu.words:
            score -= 1.5
    if qu.list_mode and "business" in f.core and "business" not in qu.words:
        score += 1.0   # listing a whole category: include business products, ranked after personal ones
    if "business" in qu.words and "business" not in f.core | f.context:
        score -= 1.5
    if qu.term and f.term == qu.term:
        score += 1.0
    elif qu.term and not f.term:
        score -= 0.5
    if qu.year and f.model_years:
        score += 1.0
    if qu.amount is not None and f.amount:
        width = f.amount[1] - f.amount[0]
        score += 1.0 + (0.5 / (1 + math.log10(1 + width)) if width != math.inf else 0)
    if qu.measure and qu.measure in f.measures:
        score += 0.25
    return score, coverage


def fmt_fact(f: Fact, prefer: str = None) -> str:
    order = ([prefer] if prefer in f.measures else []) + [m for m in MEASURE_ORDER if m in f.measures and m != prefer]
    vals = ", ".join(f"{f.measures[m]} {m}" if m not in ("value",) else f.measures[m] for m in order)
    return f"{f.label}: {vals}" if f.label else vals


REFUSE_RATE = ("That rate isn't published on Dupaco's website, so I can't quote it. "
               "Please contact Dupaco directly for a current quote.")
REFUSE_GENERAL = "I don't know. That isn't listed on Dupaco's website; please contact Dupaco for help."


def no_digits(text: str) -> str:
    return re.sub(r"\d", "", text)


# =============================================================================================
# Knowledge system
# =============================================================================================

class Knowledge:
    def __init__(self):
        self.snapshots = []                 # [(taken_on, snapshot_id)]
        self.docs: list = []                # Doc per unique (url, content) version
        self.doc_by_key = {}                # (canon url, md5) -> doc index
        self.present = {}                   # sid -> {canon url: doc idx}
        self.view = {}                      # sid -> {canon url: (doc idx, present?)}
        self.sents: list = []               # Sent
        self.postings = defaultdict(list)   # token -> [sent idx]
        self.fact_view = {}                 # sid -> [Fact]
        self._cache = {}
        self._conflicts = None
        self._last_view = {}

    # ------------------------------------------------------------------ ingest
    def ingest(self, snapshot: Snapshot) -> None:
        sid, day = snapshot.snapshot_id, snapshot.taken_on
        present = {}
        for p in snapshot.pages:
            cu = canon_url(p.url)
            key = (cu, hashlib.md5(p.markdown.encode("utf-8")).hexdigest())
            if key not in self.doc_by_key:
                self.doc_by_key[key] = self._parse_doc(p, sid, day)
            present[cu] = self.doc_by_key[key]
        self.present[sid] = present
        view = {u: (d, False) for u, (d, _) in self._last_view.items()}
        view.update({u: (d, True) for u, d in present.items()})
        self.view[sid] = view
        self._last_view = view
        facts = []
        for u, (d, here) in view.items():
            doc = self.docs[d]
            for f in doc.facts:
                facts.append(replace(f, snapshot_id=doc.snapshot_id, taken_on=doc.taken_on, present=here,
                                     authority=f.authority if here else f.authority - 30))
        self.fact_view[sid] = facts
        self.snapshots.append((day, sid))
        self.snapshots.sort()
        self._cache.clear()
        self._conflicts = None

    def _parse_doc(self, p, sid, day) -> int:
        auth = authority(p.url)
        md = p.markdown
        doc = Doc(url=canon_url(p.url), title=p.title or "", authority=auth, pub=pub_date(md), snapshot_id=sid, taken_on=day)
        idx = len(self.docs)
        self.docs.append(doc)
        if auth == 0:
            return idx
        lines = md.split("\n")
        if "%" in md or "$" in md:
            doc.facts = extract_facts(md, doc.url, doc.title, auth)
        if re.search(r"\*\*\s*(lobby|drive)", md, re.I):
            doc.hours = parse_hours(lines)
        if url_path(doc.url) == "/contact":
            doc.branches = parse_locator(lines)
        doc.routing = ROUTING_RE.findall(md)
        title_toks = set(tokens(doc.title))
        heading = frozenset(title_toks)
        self._head_text, self._title = "", frozenset(title_toks)
        for line in lines:
            s = line.strip()
            if not s or s.startswith(("|", "![", "[![")):
                continue
            if s.startswith("#"):
                heading = frozenset(set(tokens(clean_inline(s))) | title_toks)
                txt = clean_inline(s)
                self._head_text = txt if len(txt) <= 80 else ""
                if txt.endswith("?"):           # FAQ question headings are searchable too
                    self._add_sent(idx, doc, txt, heading)
                continue
            found = []
            txt = clean_inline(s, found)
            for anchor, url in found:
                doc.links.append((anchor, url, txt[:300]))
            if len(txt) < 12 or txt.count("|") > 2:
                continue
            for sent in SENT_SPLIT.split(txt):
                sent = sent.strip()
                if 12 <= len(sent) <= 600:
                    self._add_sent(idx, doc, sent, heading)
        return idx

    def _add_sent(self, idx, doc, text, heading):
        toks = frozenset(tokens(text))
        if not toks:
            return
        sidx = len(self.sents)
        self.sents.append(Sent(idx, text, toks, heading, self._head_text, self._title))
        doc.sent_ids.append(sidx)
        for t in toks | heading:
            self.postings[t].append(sidx)

    def _snapshot_for(self, as_of):
        if not self.snapshots:
            return None
        if not as_of:
            return self.snapshots[-1][1]
        i = bisect_right([d for d, _ in self.snapshots], as_of[:10])
        return self.snapshots[max(i - 1, 0)][1]

    # ------------------------------------------------------------------ answer
    def answer(self, question, as_of=None):
        q = normalize_question(question or "")
        if not as_of:
            qd = question_date(q)
            if qd and self.snapshots and qd < self.snapshots[-1][0]:
                as_of = qd
        sid = self._snapshot_for(as_of)
        if sid is None:
            return Answer(text=REFUSE_GENERAL, confident=False)
        key = (q.lower().rstrip("?.! "), sid)
        if key not in self._cache:
            self._cache[key] = self._answer(q, sid)
        a = self._cache[key]
        return Answer(text=a.text, sources=list(a.sources), confident=a.confident, conflict_note=a.conflict_note)

    def _answer(self, q: str, sid: str) -> Answer:
        if ROUTING_Q.search(q):
            a = self._routing_answer(sid)
            if a:
                return a
        if LINK_Q.search(q):
            a = self._link_answer(q, sid)
            if a:
                return a
        if HOURS_Q.search(q):
            a = self._hours_answer(q, sid)
            if a:
                return a
        if ADDRESS_Q.search(q) and re.search(r"\bbranch|office|location", q, re.I):
            a = self._address_answer(q, sid)
            if a:
                return a
        if RATE_Q.search(q) or FEE_Q.search(q):
            a = self._fact_answer(q, sid)
            if a:
                return a
            if RATE_ASK.search(q) and not re.search(r"\b(how|why|what is a|what's a|explain|difference)\b", q, re.I):
                a = self._rate_sentence(q, sid)
                return a or Answer(text=REFUSE_RATE, confident=False)
        return self._search_answer(q, sid)

    # ---- routing number
    def _routing_answer(self, sid):
        votes = Counter()
        src = {}
        for u, (d, here) in self.view[sid].items():
            doc = self.docs[d]
            for num in doc.routing:
                w = (doc.authority + 10) * (1.0 if here else 0.5)
                votes[num] += w
                if num not in src or doc.authority > self.docs[src[num]].authority:
                    src[num] = d
        if not votes:
            return None
        num = sorted(votes, key=lambda n: (-votes[n], n))[0]
        doc = self.docs[src[num]]
        return Answer(text=f"Dupaco's routing number is {num}.",
                      sources=[Source(url=doc.url, snapshot_id=doc.snapshot_id, quote=f"routing number {num}")])

    # ---- links
    def _link_answer(self, q, sid):
        if RATE_Q.search(q):    # "link to see certificate rates" -> the product page holding those rates
            f = self._best_fact_page(re.sub(r"\b(link|url|web ?address|web ?page|website|page|use|should|see|visit|go)\b",
                                            " ", q, flags=re.I), sid)
            if f:
                return Answer(text=f"You can see them at {f.url}#rates", sources=[Source(url=f.url, snapshot_id=f.snapshot_id)])
        qt = [t for t in tokens(q) if t not in GENERIC and t not in ("link", "url", "web", "address", "website",
                                                                       "page", "online", "use", "should", "site")]
        if not qt:
            return None
        votes, quote = Counter(), {}
        for u, (d, here) in self.view[sid].items():
            doc = self.docs[d]
            if doc.authority < 10:
                continue
            for anchor, url, ctx in doc.links:
                pu = parse_url(url)
                host = pu.netloc.lower()
                if "dupaco.com" not in host or host.startswith(("info.", "shine.", "homeloans.")):
                    continue
                at, ct = set(tokens(anchor + " " + pu.path.replace("-", " "))), set(tokens(ctx))
                s = sum(1.0 for t in qt if token_hit(t, at)) + sum(0.3 for t in qt if token_hit(t, ct) and not token_hit(t, at))
                if s < 1.0:
                    continue
                clean = f"{pu.scheme}://{host}{pu.path}" if pu.path not in ("", "/") else f"{pu.scheme}://{host}/"
                if host.split(".")[0] in qt:
                    s += 2.0          # "join online" -> join.<domain>
                votes[clean] += s * (0.5 + doc.authority / 100) * (1 if here else 0.5)
                quote.setdefault(clean, (doc, anchor))
        if not votes:
            return None
        best = sorted(votes, key=lambda u: (-votes[u], u))[0]
        doc, anchor = quote[best]
        return Answer(text=f"Use {best}", sources=[Source(url=doc.url, snapshot_id=doc.snapshot_id, quote=anchor[:120])])

    # ---- hours
    def _hours_answer(self, q, sid):
        kind = "drive-up" if re.search(r"drive", q, re.I) else "lobby"
        blocks = []
        for u, (d, here) in sorted(self.view[sid].items()):
            doc = self.docs[d]
            for k, sched, ctx in doc.hours:
                if k == kind:
                    blocks.append((doc, sched, ctx))
        name = self._branch_name(q)
        if name:
            nt = {ABBR.get(t, t) for t in tokens(name)}
            hits = [b for b in blocks if nt <= {ABBR.get(t, t) for t in tokens(b[2])}]
            if not hits:
                return Answer(text=no_digits(f"I couldn't find posted {kind} hours for the {name} branch on Dupaco's "
                                             f"website. Please contact Dupaco to confirm that branch's hours."),
                              confident=False)
            doc, sched, _ = sorted(hits, key=lambda b: (b[0].pub, b[0].url), reverse=True)[0]
            return Answer(text=f"{name} branch {kind} hours: " + "; ".join(sched) + ".",
                          sources=[Source(url=doc.url, snapshot_id=doc.snapshot_id)])
        if not blocks:
            return None
        counts = Counter()
        first = {}
        for doc, sched, _ in blocks:
            counts[sched] += 1
            first.setdefault(sched, doc)
        ranked = sorted(counts, key=lambda s: (-counts[s], -len(s), s))
        label = "Lobby" if kind == "lobby" else "Drive-up"
        if SAME_Q.search(q) and len(ranked) > 1:
            a, b = ranked[0], ranked[1]
            text = (f"No. {label} hours vary by branch. One common schedule is " + "; ".join(a) +
                    ". Another is " + "; ".join(b) + ". Which branch would you like?")
            srcs = [first[a], first[b]]
        else:
            text = (f"{label} hours vary by branch. Most branches: " + "; ".join(ranked[0]) +
                    ". Which branch would you like hours for?")
            srcs = [first[ranked[0]]]
        return Answer(text=text, sources=[Source(url=d.url, snapshot_id=d.snapshot_id) for d in srcs])

    def _branch_name(self, q):
        for m in BRANCH_NAME_RX.finditer(q):
            words = [w for w in m.group(1).split() if w.lower() not in NOT_BRANCH]
            if words:
                return " ".join(words)
        return None

    # ---- branch address
    def _address_answer(self, q, sid):
        name = self._branch_name(q)
        if not name:
            return None
        nt = {ABBR.get(t, t) for t in tokens(name)}
        for u, (d, here) in sorted(self.view[sid].items(), key=lambda x: (not x[1][1], x[0])):
            doc = self.docs[d]
            for bname, addr, toks in sorted(doc.branches, key=lambda b: ("branch" not in b[0].lower(), "atm" in b[0].lower())):
                if nt <= toks:
                    return Answer(text=f"The {name} branch is at {addr}.",
                                  sources=[Source(url=doc.url, snapshot_id=doc.snapshot_id, quote=addr)])
        return Answer(text=no_digits(f"I couldn't find a Dupaco branch called {name} on the website. "
                                     f"Please contact Dupaco to confirm the location."), confident=False)

    # ---- facts (rates and fees)
    def _rank_facts(self, q, sid):
        qu = parse_question(q)
        scored = []
        for f in self.fact_view.get(sid, []):
            r = score_fact(qu, f)
            if r and r[1] >= 0.6 and r[0] > 0:
                scored.append((r[0], f))
        return qu, scored

    def _best_fact_page(self, q, sid):
        qu, scored = self._rank_facts(q, sid)
        if not scored:
            return None
        best = max(s for s, _ in scored)
        good = [f for s, f in scored if s >= best - 1.0]
        return sorted(good, key=lambda f: (-f.authority, not f.present, f.url))[0]

    def _fact_answer(self, q, sid):
        qu, scored = self._rank_facts(q, sid)
        if not scored:
            return None
        best = max(s for s, _ in scored)
        good = [(s, f) for s, f in scored if s >= best - 1.0]
        top_auth = max(f.authority for _, f in good)
        if qu.list_mode and top_auth >= 90:
            good = sorted(((s, f) for s, f in good if f.authority >= 90), key=lambda x: (-x[0], x[1].url))
            top = good[0][0]
            rows = [f for s, f in good if s >= top - 0.5]
            return self._format_facts(qu, sid, rows, good)
        good = [(s, f) for s, f in good if f.authority == top_auth]
        page_best = {}
        for s, f in good:
            page_best[f.url] = max(page_best.get(f.url, -1e9), s)
        url = sorted(page_best, key=lambda u: (-page_best[u], u))[0]
        top = page_best[url]
        rows = [f for s, f in good if f.url == url and s >= top - 1e-9]
        return self._format_facts(qu, sid, rows, good)

    def _format_facts(self, qu, sid, rows, good):
        url = rows[0].url
        if qu.superlative:
            pick = max if qu.superlative == "hi" else min
            rows = [pick(rows, key=lambda f: f.primary_value())]
        seen, uniq = set(), []
        for f in rows:
            k = (f.label or f.product, tuple(sorted(f.measures.items())))
            if k not in seen:
                seen.add(k)
                uniq.append(f)
        rows = uniq[:10]
        products = list(dict.fromkeys(f.product for f in rows))
        if len(products) == 1:
            body = "; ".join(fmt_fact(f, qu.measure) for f in rows)
            text = f"{products[0]} \u2014 {body}" if products[0] and rows[0].label else (
                f"{products[0]}: {body}" if products[0] else body)
        else:
            text = "; ".join(f"{f.product} \u2014 {fmt_fact(f, qu.measure)}" if f.product and f.label else
                             (f"{f.product}: {fmt_fact(f, qu.measure)}" if f.product else fmt_fact(f, qu.measure))
                             for f in rows)
        if rows[0].effective:
            text += f" (rates effective {rows[0].effective})"
        text += f". Source: {url}"
        shown = {(f.label, tuple(sorted(f.measures.items()))) for f in rows}
        urls = sorted({f.url for _, f in good if f.url != url and f.authority >= rows[0].authority
                       and (f.label, tuple(sorted(f.measures.items()))) in shown})
        urls = list(dict.fromkeys([url] + sorted({f.url for f in rows}) + urls))[:5]
        snap = {f.url: f.snapshot_id for f in rows}
        note = self._conflict_note(rows, sid)
        return Answer(text=text, sources=[Source(url=u, snapshot_id=snap.get(u, sid), quote=text[:200]) for u in urls],
                      conflict_note=note)

    def _conflict_note(self, rows, sid):
        notes = []
        for c in self._conflicts_for(sid):
            for f in rows:
                if c.resolution and c.resolution in f.measures.values() and c.topic.startswith(f.product):
                    notes.append(f"{c.topic}: other pages/snapshots show {', '.join(v for v in c.values if v != c.resolution)}; "
                                 f"used {c.resolution} because {c.reason}.")
                    break
        return " ".join(dict.fromkeys(notes)) or None

    def _rate_sentence(self, q, sid):
        """A rate asked about in prose only: accept a sentence quoting a rate that mentions every product word."""
        qu = parse_question(q)
        if not qu.content:
            return None
        rate_rx = re.compile(r"\d+(?:\.\d+)?%\s*(?:apr|apy|annual percentage|fixed|variable|interest|dividend)|"
                             r"rates? (?:of|as low as|at|is|are) \d", re.I)
        best = None
        for sidx in self._candidates(qu.content, sid):
            s = self.sents[sidx]
            if not rate_rx.search(s.text):
                continue
            if all(token_hit(t, s.toks) for t in qu.content):
                doc = self.docs[s.doc]
                k = (doc.authority, doc.pub, -sidx)
                if best is None or k > best[0]:
                    best = (k, s, doc)
        if not best:
            return None
        _, s, doc = best
        return Answer(text=s.text, sources=[Source(url=doc.url, snapshot_id=doc.snapshot_id, quote=s.text[:200])])

    # ---- sentence search
    def _active(self, sid):
        act = {}
        for u, (d, here) in self.view[sid].items():
            act[d] = here
        return act

    def _candidates(self, qtoks, sid):
        act = self._active(sid)
        out = set()
        for t in qtoks:
            for tt in {t} | SYNONYMS.get(t, set()):
                for sidx in self.postings.get(tt, ()):
                    if self.sents[sidx].doc in act:
                        out.add(sidx)
        return sorted(out)

    def _idf(self, t):
        df = len(self.postings.get(t, ())) or 1
        return math.log(1 + len(self.sents) / df)

    @staticmethod
    def _counts_noun(text, qtoks):
        """True if a number directly precedes one of the question's nouns ('22 branch', '294 Dupaco employees')."""
        words = re.findall(r"[\w,'-]+", text.lower())
        for i, w in enumerate(words):
            if re.fullmatch(r"\d[\d,]*", w):
                for nxt in words[i + 1:i + 3]:
                    if stem(nxt.strip(",'")) in qtoks:
                        return True
        return False

    def _search_answer(self, q, sid):
        qtoks = [t for t in dict.fromkeys(tokens(q)) if t not in GENERIC and len(t) > 1]
        if not qtoks:
            return Answer(text=REFUSE_GENERAL, confident=False)
        act = self._active(sid)
        weights = {t: self._idf(t) for t in qtoks}
        total = sum(weights.values())
        count_q = bool(re.search(r"\bhow (many|much)\b|\bnumber of\b", q, re.I))
        scored = []
        for sidx in self._candidates(qtoks, sid):
            s = self.sents[sidx]
            doc = self.docs[s.doc]
            sc, cov = 0.0, 0.0
            for t in qtoks:
                if token_hit(t, s.toks):
                    sc += weights[t]
                    cov += weights[t]
                elif token_hit(t, s.head):
                    sc += 0.4 * weights[t]
                    cov += 0.4 * weights[t]
            if sc <= 0:
                continue
            w = (0.7 + 0.3 * doc.authority / 100) * (1.0 if act[s.doc] else 0.7)
            w *= 1.0 if len(s.text) < 350 else 0.85
            # the page title supplies context the sentence lacks (a "flexible options" line on the Student Loans page)
            w *= 1.0 + 0.2 * sum(1 for t in qtoks if token_hit(t, s.title) and not token_hit(t, s.toks))
            if s.text.endswith("?") and sc >= 0.7 * total:
                w *= 1.35         # an FAQ question matching the member's question; its answer follows it
            scored.append((sc * w, cov / total, sidx))
        if not scored:
            return Answer(text=REFUSE_GENERAL, confident=False)
        best = max(x[0] for x in scored)
        best_cov = max(x[1] for x in scored if x[0] == best)
        if best_cov < 0.45:
            return Answer(text=REFUSE_GENERAL, confident=False)
        pool = scored
        if OFFER_Q.search(q):
            # "do you have/offer any X": a question about products, answered from product pages when they are relevant
            prod = [x for x in scored if self.docs[self.sents[x[2]].doc].authority >= 90 and x[1] >= 0.3]
            pool = prod or pool
        if count_q:
            # "how many X": only sentences stating "<number> ... X"; counts change over time and only dated
            # statements can be ordered, so dated pages are preferred
            counted = [x for x in scored if self._counts_noun(self.sents[x[2]].text, qtoks)]
            if counted:
                dated = [x for x in counted if self.docs[self.sents[x[2]].doc].pub]
                pool = dated or counted
        top = max(x[0] for x in pool)
        # near-ties (the same fact repeated across pages/years): the most recently published page wins
        cands = [x for x in pool if x[0] >= (0.75 if count_q else 0.9) * top]

        self._last_cands = cands     # kept for debugging only

        def own_cov(sidx):     # share of the question the sentence itself states (not its heading/title)
            st = self.sents[sidx].toks
            return round(sum(weights[t] for t in qtoks if token_hit(t, st)) / total, 3)

        def rank(x):
            doc = self.docs[self.sents[x[2]].doc]
            # undated pages that are live in this view are current; dated articles are ordered by publication
            when = doc.pub or "9999"
            return (when, own_cov(x[2]), x[0], doc.authority, -x[2])
        _, _, sidx = max(cands, key=rank)
        s = self.sents[sidx]
        doc = self.docs[s.doc]
        ids = doc.sent_ids
        pos = ids.index(sidx)
        # complete the answer: add the neighbouring sentence (±2) that covers the most query words the best
        # sentence misses (e.g. "...Hy-Vee branch employees..." + "This location will close March 31, 2026.")
        missing = [t for t in qtoks if not token_hit(t, s.toks)]
        extra, extra_score = None, 0.0
        for k in (pos - 1, pos + 1, pos - 2, pos + 2):
            if 0 <= k < len(ids) and missing:
                nb = self.sents[ids[k]]
                sc = sum(weights[t] for t in missing if token_hit(t, nb.toks))
                if sc > extra_score + 1e-9:
                    extra, extra_score = k, sc
        if extra is None:
            if re.match(r"^(this|it|that|these|they|he|she)\b", s.text, re.I) and pos > 0:
                extra = pos - 1
            elif (len(s.text) < 80 or s.text.endswith("?")) and pos + 1 < len(ids):
                extra = pos + 1
        order = sorted({pos} | ({extra} if extra is not None else set()))
        text = " ".join(self.sents[ids[k]].text for k in order)[:700]
        # give a bare sentence its subject: prefix the section heading when it names what was asked
        shown = set().union(*(self.sents[ids[k]].toks for k in order))
        if s.head_text and not s.text.endswith("?") and any(
                token_hit(t, frozenset(tokens(s.head_text))) and not token_hit(t, shown) for t in qtoks):
            text = f"{s.head_text}: {text}"
        return Answer(text=text, sources=[Source(url=doc.url, snapshot_id=doc.snapshot_id, quote=s.text[:200])])

    # ------------------------------------------------------------------ conflicts
    def conflicts(self):
        if not self.snapshots:
            return []
        return [replace(c) for c in self._conflicts_for(self.snapshots[-1][1])]

    def _conflicts_for(self, sid):
        if self._conflicts is not None and self._conflicts[0] == sid:
            return self._conflicts[1]
        out = self._fact_conflicts(sid) + self._claim_conflicts(sid)
        self._conflicts = (sid, out)
        return out

    @staticmethod
    def _norm_tokens(f: Fact):
        drop = {"month", "year", "model", "mo", "yr", "rate", "standard", "conventional", "fixed", "purchase", "or",
                "less", "and", "older", "the", "of", "loan", "loans"}
        return {t for t in f.core if not t.isdigit() and t not in drop}

    def _fact_conflicts(self, sid):
        """(a) a lower-authority page in the current view states a different value for the same product/tier;
        (b) an authoritative page's value changed between snapshots."""
        facts = [f for f in self.fact_view[sid] if f.present]
        auth = [f for f in facts if f.authority >= 90]
        groups = {}

        def gkey(a: Fact):
            m = a.main_measure()
            return (a.product, a.label, m)

        def add(a: Fact, other_val: str, src: Source, reason: str):
            k = gkey(a)
            g = groups.setdefault(k, {"fact": a, "values": {}, "reasons": []})
            g["values"].setdefault(a.measures[a.main_measure()], Source(url=a.url, snapshot_id=a.snapshot_id))
            g["values"].setdefault(other_val, src)
            if reason not in g["reasons"]:
                g["reasons"].append(reason)

        # (a) stale copies on weaker pages
        for low in facts:
            if low.authority >= 90 or low.authority <= 0:
                continue
            m = low.main_measure()
            lt = self._norm_tokens(low)
            best, best_j = None, 0.0
            for a in auth:
                if m not in a.measures or a.term != low.term:
                    continue
                if (a.my_rank is None) != (low.my_rank is None) or (a.my_rank is not None and a.my_rank != low.my_rank):
                    continue
                if low.amount and a.amount and low.amount != a.amount:
                    continue
                at = self._norm_tokens(a)
                if not lt or not at:
                    continue
                j = len(lt & at) / len(lt | at)
                if j > best_j or (j == best_j and best is not None and (a.amount or (0, 0))[1] < (best.amount or (0, 0))[1]):
                    best, best_j = a, j
            if best is None or best_j < 0.5:
                continue
            if low.measures[m] != best.measures[m]:
                kind = "calculator" if low.authority == 30 else "promotion" if low.authority == 40 else \
                    "blog/newsroom" if low.authority <= 10 else "secondary"
                add(best, low.measures[m], Source(url=low.url, snapshot_id=low.snapshot_id),
                    f"the product rate page{' (effective ' + best.effective + ')' if best.effective else ''} is authoritative; "
                    f"the {kind} page is stale")
        # (b) changed between snapshots on the same authoritative page
        # key on (page, row label, measure): table captions get reworded between crawls; labels that are not
        # unique on a page (e.g. "Standard" in two card tables) are skipped to avoid false matches
        def ckey(f):
            return (f.url, re.sub(r"\W+", " ", f.label or f.product).lower().strip(), f.main_measure())
        counts = Counter(ckey(f) for f in auth)
        cur = {ckey(f): f for f in auth if counts[ckey(f)] == 1}
        sid_day = dict((s, d) for d, s in self.snapshots)[sid]
        for day, osid in self.snapshots:
            if osid == sid or day > sid_day:
                continue
            ocounts = Counter(ckey(f) for f in self.fact_view[osid] if f.present and f.authority >= 90)
            for f in self.fact_view[osid]:
                if not f.present or f.authority < 90:
                    continue
                k = ckey(f)
                a = cur.get(k)
                if a and ocounts[k] == 1 and a.measures[k[2]] != f.measures.get(k[2], a.measures[k[2]]):
                    add(a, f.measures[k[2]], Source(url=f.url, snapshot_id=f.snapshot_id),
                        f"newer snapshot {a.snapshot_id} shows the updated value"
                        f"{' (effective ' + a.effective + ')' if a.effective else ''}")
        def variant(kv):   # report the plain personal product; business/IRA copies of the same change merge into it
            words = set(tokens(f"{kv[0][0]} {kv[0][1]}"))
            return (len(words & {"business", "ira", "roth", "traditional"}), kv[0])

        out, seen = [], set()
        for (product, label, m), g in sorted(groups.items(), key=variant):
            a = g["fact"]
            nums = [frozenset(PCT_RE.findall(v) or re.findall(r"\d[\d,.]*", v)) for v in g["values"]]
            if any(x < y for x in nums for y in nums):
                continue          # "2.90%" vs "2.90%–12.50%": the same cell parsed two ways, not a contradiction
            vals = tuple(sorted(g["values"]))
            dedup = (m, vals)     # the same rate change shown in several tables (personal, business, IRA)
            if dedup in seen:
                continue
            seen.add(dedup)
            term = f" {a.term}-month" if a.term else ""
            topic = f"{product} {label}{term} {m}".replace("  ", " ").strip()
            out.append(Conflict(topic=topic, values=list(g["values"]), sources=list(g["values"].values()),
                                resolution=a.measures[m], reason="; ".join(g["reasons"])))
        return out

    CLAIMS = [
        ("number of Dupaco branch locations",
         re.compile(r"(?<![\d,.])\b(\d{1,3})\s+(?:full-service\s+)?branch(?:es| office locations| locations)\b", re.I), "{} branch locations"),
        ("number of times Forbes recognized Dupaco",
         re.compile(r"\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth) time Forbes", re.I), "{} time Forbes recognized Dupaco"),
    ]

    def _claim_conflicts(self, sid):
        out = []
        for topic, rx, fmt in self.CLAIMS:
            seen = {}
            for u, (d, here) in sorted(self.view[sid].items()):
                if not here:
                    continue
                doc = self.docs[d]
                if doc.authority <= 0:
                    continue
                for sidx in doc.sent_ids:
                    for m in rx.finditer(self.sents[sidx].text):
                        v = fmt.format(m.group(1).lower())
                        prev = seen.get(v)
                        key = (doc.pub or "", doc.url)
                        if prev is None or key > prev[0]:
                            seen[v] = (key, doc)
            if len(seen) < 2:
                continue
            winner = max(seen, key=lambda v: seen[v][0])
            out.append(Conflict(topic=topic, values=sorted(seen),
                                sources=[Source(url=seen[v][1].url, snapshot_id=seen[v][1].snapshot_id) for v in sorted(seen)],
                                resolution=winner,
                                reason=f"most recently published page ({seen[winner][0][0] or 'undated'}) wins over older releases"))
        return out


def build():
    return Knowledge()
