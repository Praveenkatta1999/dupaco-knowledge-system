"""
mysolution — knowledge system for the Dupaco snapshots.

Step 1: keep every page from every snapshot; answer from the newest snapshot on/before `as_of`.
Step 2: parse every rate table into Fact records at ingest; answer rate/APY/APR questions by
        matching product + qualifiers and picking the most authoritative page. Other questions
        fall back to the naive word-overlap search (replaced in later steps).
"""
import hashlib
import math
import re
import sys
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(next(d / "harness" for d in Path(__file__).resolve().parents if (d / "harness" / "contract.py").exists())))
from contract import Answer, Conflict, Snapshot, Source  # noqa: E402

WORD = re.compile(r"[a-z0-9$%.]+")


# ---------------------------------------------------------------------------------------------
# Pages and authority
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class PageRec:
    url: str
    snapshot_id: str
    taken_on: str
    markdown: str


def url_path(url: str) -> str:
    return urlparse(url if "//" in url else "https://" + url).path.rstrip("/").lower()


def authority(url: str) -> int:
    """How much to trust a page for rates. Product pages beat rate sheets beat promos/calculators/blogs."""
    p = url_path(url)
    if p.startswith(("/newsroom/tag", "/newsroom/author", "/business/resources/tag")):
        return 0
    if p.startswith(("/learn", "/newsroom", "/business/resources", "/blog")) or re.match(r"^/\d{4}/", p):
        return 10
    if "calculator" in p:
        return 30
    if p.endswith("-lp") or p.startswith(("/switch-and-earn", "/earn-more")):
        return 40
    if p.startswith(("/personal/", "/business/")) or p in ("/checking",):
        return 100
    if p in ("/rates", "/business-rates"):
        return 95
    if p.startswith(("/about", "/contact")) or p == "":
        return 80
    return 50


# ---------------------------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------------------------

TOKEN = re.compile(r"[a-z0-9]+")
SYNONYMS = {
    "cd": {"certificate"}, "cds": {"certificate"}, "certificates": {"certificate"},
    "mortgage": {"mortgage", "conventional"}, "mortgages": {"mortgage", "conventional"},
    "car": {"auto"}, "vehicle": {"auto"}, "automobile": {"auto"},
    "heloc": {"heloc", "equity"}, "boat": {"boat"}, "rv": {"rv"},
    "card": {"visa"}, "cards": {"visa"}, "credit": {"visa"},
}
# Words that carry no product meaning in a rate question.
GENERIC = set("""
a an the is are was were be what whats what's which who how much many do does did can could would will
you your i me my we our us it its of on in at to for from with and or about any all some this that these
there their dupaco dupacos rate rates interest apy apr yield dividend dividends annual percentage current
currently right now today tell give show provide list know like want please get earn earns pay pays paying
offer offers account accounts s on'
balance between tier tiers level levels amount type types kind kinds option options available
""".split())
# Qualifier words that make a row a *different* product when the question doesn't mention them.
DISTINGUISH = {"business", "jumbo", "roth", "traditional", "ira", "refinance", "introductory", "intro",
               "transfer", "health", "mini", "max", "minimax"}
SUPER_HI = {"highest", "best", "top", "maximum", "max", "most"}
SUPER_LO = {"lowest", "cheapest", "minimum", "least"}


def stem(t: str) -> str:
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


def tokens(text: str) -> list:
    return [stem(t) for t in TOKEN.findall(text.lower())]


TERM_RE = re.compile(r"(\d{1,3})\s*[- ]?\s*(months?|mos?|years?|yrs?)\b", re.I)
MONEY_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
YEAR_RANGE_RE = re.compile(r"\b((?:19|20)\d\d)\s*[-–—/]\s*(\d{2,4})\b")
PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
EFFECTIVE_RE = re.compile(r"effective as of\s+(\d{1,2}/\d{1,2}/\d{4})", re.I)


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
    if not nums:
        m = re.search(r"\$?\s?(\d{1,3}(?:,\d{3})+)\s*[-–]\s*(\d{1,3}(?:,\d{3})+)", text)
        if not m:
            return None
        nums = [money(m.group(1)), money(m.group(2))]
    if len(nums) == 1:
        # '$10,000-24,999' style: second number without '$'
        m = re.search(r"\$\s?[\d,]+(?:\.\d+)?\s*[-–]\s*([\d,]{4,}(?:\.\d+)?)", text)
        if m:
            nums.append(money(m.group(1)))
    if len(nums) >= 2:
        return (min(nums[:2]), max(nums[:2]))
    lo = nums[0]
    if re.search(r"or less|and under|up to|or below", text, re.I):
        return (0.0, lo)
    return (lo, math.inf)


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


# ---------------------------------------------------------------------------------------------
# Rate-table facts
# ---------------------------------------------------------------------------------------------

@dataclass
class Fact:
    product: str                 # caption / table product name
    label: str                   # row label (product, tier, term...)
    measures: dict               # {"APY": "0.20%", "dividend rate": "0.20%", ...}
    url: str
    snapshot_id: str
    taken_on: str
    effective: str = ""
    authority: int = 50
    table_id: int = 0
    term: int = None
    amount: tuple = None
    model_years: tuple = None
    core: set = field(default_factory=set)      # tokens from product + label (strong)
    context: set = field(default_factory=set)   # tokens from section, page title, url (weak)

    def primary_value(self) -> float:
        for k in ("APY", "APR", "rate", "dividend rate"):
            if k in self.measures:
                return float(PCT_RE.search(self.measures[k]).group(1))
        return 0.0


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


def clean_value(cell: str) -> str:
    vals = PCT_RE.findall(cell)
    if not vals:
        return ""
    return "–".join(f"{v}%" for v in vals[:2])


def extract_facts(rec: PageRec, title: str, auth: int) -> list:
    lines = rec.markdown.split("\n")
    facts, tid = [], 0
    ctx_tokens = set(tokens(title)) | set(tokens(url_path(rec.url).replace("-", " ")))
    i = 0
    while i < len(lines) - 1:
        if not (lines[i].startswith("|") and TABLE_SEP.match(lines[i + 1])):
            i += 1
            continue
        header = split_row(lines[i])
        j = i + 2
        rows = []
        while j < len(lines) and lines[j].startswith("|"):
            rows.append(split_row(lines[j]))
            j += 1
        tid += 1
        kinds = [measure_name(h) for h in header]
        if not any(k not in (None, "skip") for k in kinds) or not any("%" in "".join(r) for r in rows):
            i = j
            continue
        # caption (nearest plain line above) and section (the one above that)
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
        for k in range(j, min(len(lines), j + 8)):
            m = EFFECTIVE_RE.search(lines[k])
            if m:
                eff = m.group(1)
                break
        for r in rows:
            if len(r) != len(header):
                continue
            measures, label_parts = {}, []
            for h, kind, cell in zip(header, kinds, r):
                if kind == "skip":
                    continue
                if kind:
                    v = clean_value(cell)
                    if v:
                        measures[kind] = v
                elif cell and cell not in ("—", "-"):
                    label_parts.append(cell)
            if not measures:
                continue
            label = " | ".join(label_parts)
            core = set(tokens(caption)) | set(tokens(label))
            facts.append(Fact(
                product=caption, label=label, measures=measures, url=rec.url, snapshot_id=rec.snapshot_id,
                taken_on=rec.taken_on, effective=eff, authority=auth, table_id=tid,
                term=term_months(label) or term_months(caption), amount=amount_range(label),
                model_years=model_year_range(label), core=core,
                context=(set(tokens(section)) | ctx_tokens) - core,
            ))
        i = j
    return facts


# ---------------------------------------------------------------------------------------------
# Question parsing and rate matching
# ---------------------------------------------------------------------------------------------

RATE_Q = re.compile(r"\b(rates?|apy|apr|yield|dividends?|interest|%)", re.I)
MEASURE_Q = [("APY", re.compile(r"\bapy\b|annual percentage yield|\byield\b", re.I)),
             ("APR", re.compile(r"\bapr\b|annual percentage rate", re.I)),
             ("dividend rate", re.compile(r"dividend", re.I))]
MEASURE_ORDER = ["rate", "dividend rate", "APY", "APR"]


@dataclass
class Query:
    content: list          # product tokens (stemmed, no generic words, no numbers)
    term: int = None
    amount: float = None
    year: int = None
    measure: str = None
    superlative: str = None
    words: set = field(default_factory=set)
    list_mode: bool = False   # "what cards do you offer", "rates for the credit cards": include every product


LIST_Q = re.compile(r"\b(what|which)\b.*\b(do you|does dupaco) (offer|have)\b|\ball\b|\b(the|your) [a-z ]*(cards|loans|accounts|certificates)\b", re.I)


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
    if words & SUPER_HI:
        qu.superlative = "hi"
    elif words & SUPER_LO:
        qu.superlative = "lo"
    # term words like "month"/"year" are qualifiers, not product words
    qu.content = [t for t in qu.content if t not in ("month", "year", "mo", "yr", "model")]
    return qu


def token_hit(t: str, bag: set) -> bool:
    return t in bag or bool(SYNONYMS.get(t, set()) & bag)


def score_fact(qu: Query, f: Fact):
    """Return (score, coverage) or None if the fact contradicts a qualifier in the question."""
    if qu.term and f.term and qu.term != f.term:
        return None
    if qu.year and f.model_years and not (f.model_years[0] <= qu.year <= f.model_years[1]):
        return None
    if qu.amount is not None and f.amount and not (f.amount[0] <= qu.amount <= f.amount[1]):
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
        score += 1.0   # listing everything: include business products, ranked after personal ones
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
    vals = ", ".join(f"{f.measures[m]} {m if m != 'rate' else 'rate'}" for m in order)
    return f"{f.label}: {vals}" if f.label else vals


# ---------------------------------------------------------------------------------------------
# Knowledge system
# ---------------------------------------------------------------------------------------------

class Knowledge:
    def __init__(self):
        self.snapshots = []   # [(taken_on, snapshot_id)], sorted by date
        self.pages = {}       # snapshot_id -> list[PageRec]
        self.facts = {}       # snapshot_id -> list[Fact]
        self._fact_cache = {} # (url, content hash) -> facts parsed once, re-stamped per snapshot

    def ingest(self, snapshot: Snapshot) -> None:
        sid, day = snapshot.snapshot_id, snapshot.taken_on
        recs, facts = [], []
        for p in snapshot.pages:
            rec = PageRec(p.url, sid, day, p.markdown)
            recs.append(rec)
            auth = authority(p.url)
            if auth == 0 or "%" not in p.markdown:
                continue
            key = (p.url, hashlib.md5(p.markdown.encode("utf-8")).hexdigest())
            if key not in self._fact_cache:
                self._fact_cache[key] = extract_facts(rec, p.title, auth)
            for f in self._fact_cache[key]:
                facts.append(Fact(**{**f.__dict__, "snapshot_id": sid, "taken_on": day}))
        self.pages[sid] = recs
        self.facts[sid] = facts
        self.snapshots.append((day, sid))
        self.snapshots.sort()

    def _snapshot_for(self, as_of):
        """Newest snapshot taken on or before as_of; the oldest one if as_of predates them all."""
        if not self.snapshots:
            return None
        if not as_of:
            return self.snapshots[-1][1]
        i = bisect_right([d for d, _ in self.snapshots], as_of[:10])
        return self.snapshots[max(i - 1, 0)][1]

    # --- rate route ---------------------------------------------------------------------------
    def _rate_answer(self, question: str, sid: str):
        if not RATE_Q.search(question):
            return None
        qu = parse_question(question)
        scored = []
        for f in self.facts.get(sid, []):
            r = score_fact(qu, f)
            if r and r[1] >= 0.6 and r[0] > 0:
                scored.append((r[0], f))
        if not scored:
            return None
        best = max(s for s, _ in scored)
        good = [(s, f) for s, f in scored if s >= best - 1.0]
        top_auth = max(f.authority for _, f in good)
        if qu.list_mode and top_auth >= 90:
            # listing a whole category: any rate sheet / product page counts (personal cards live
            # only on /rates, business cards on /business/credit-cards); best-scoring rows first
            good = sorted(((s, f) for s, f in good if f.authority >= 90), key=lambda x: (-x[0], x[1].url))
            top = good[0][0]
            rows = [f for s, f in good if s >= top - 0.5]
            return self._format_rates(qu, sid, rows, good)
        good = [(s, f) for s, f in good if f.authority == top_auth]
        # one page: the one holding the best-scoring row (ties -> alphabetical url for determinism)
        page_best = {}
        for s, f in good:
            page_best[f.url] = max(page_best.get(f.url, -1e9), s)
        url = sorted(page_best, key=lambda u: (-page_best[u], u))[0]
        top = page_best[url]
        rows = [f for s, f in good if f.url == url and s >= top - 1e-9]
        return self._format_rates(qu, sid, rows, good)

    def _format_rates(self, qu, sid, rows, good):
        url = rows[0].url
        if qu.superlative:
            pick = max if qu.superlative == "hi" else min
            rows = [pick(rows, key=lambda f: f.primary_value())]
        # de-duplicate identical rows (same table repeated on a page)
        seen, uniq = set(), []
        for f in rows:
            k = (f.label, tuple(sorted(f.measures.items())))
            if k not in seen:
                seen.add(k)
                uniq.append(f)
        rows = uniq[:10]
        products = list(dict.fromkeys(f.product for f in rows))
        if len(products) == 1:
            body = "; ".join(fmt_fact(f, qu.measure) for f in rows)
            text = f"{products[0]} — {body}" if products[0] else body
        else:
            body = "; ".join(f"{f.product} — {fmt_fact(f, qu.measure)}" if f.product else fmt_fact(f, qu.measure)
                             for f in rows)
            text = body
        eff = rows[0].effective
        if eff:
            text += f" (rates effective {eff})"
        text += f". Source: {url}"
        # cite every equally-authoritative page that shows the same values (same table on several pages)
        shown = {(f.label, tuple(sorted(f.measures.items()))) for f in rows}
        urls = [url] + sorted({f.url for _, f in good if f.url != url
                               and (f.label, tuple(sorted(f.measures.items()))) in shown})
        urls = list(dict.fromkeys([url] + sorted({f.url for f in rows}) + urls))[:5]
        return Answer(text=text, sources=[Source(url=u, snapshot_id=sid, quote=body[:200]) for u in urls])

    # --- fallback: naive search ---------------------------------------------------------------
    def _search_answer(self, question: str, sid: str):
        q = set(WORD.findall(question.lower()))
        best, best_score = None, 0
        for rec in self.pages[sid]:
            for para in rec.markdown.split("\n\n"):
                s = len(q & set(WORD.findall(para.lower())))
                if s > best_score:
                    best, best_score = (rec, para.strip()), s
        if not best:
            return Answer(text="I don't know.", confident=False)
        rec, para = best
        return Answer(text=para[:600], sources=[Source(url=rec.url, snapshot_id=rec.snapshot_id)])

    def answer(self, question, as_of=None):
        sid = self._snapshot_for(as_of)
        if sid is None:
            return Answer(text="I don't know.", confident=False)
        return self._rate_answer(question, sid) or self._search_answer(question, sid)

    def conflicts(self):
        return []


def build():
    return Knowledge()
