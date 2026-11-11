"""
mysolution — step 1: keep every page from every snapshot and answer from the snapshot
that matches `as_of` (newest snapshot taken on or before that date; latest when as_of is None).

Retrieval is still the starter's naive word-overlap search; later steps replace it.
"""
import re
import sys
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(next(d / "harness" for d in Path(__file__).resolve().parents if (d / "harness" / "contract.py").exists())))
from contract import Answer, Conflict, Snapshot, Source  # noqa: E402

WORD = re.compile(r"[a-z0-9$%.]+")


@dataclass(frozen=True)
class PageRec:
    url: str
    snapshot_id: str
    taken_on: str
    markdown: str


class Knowledge:
    def __init__(self):
        self.snapshots = []   # [(taken_on, snapshot_id)], sorted by date
        self.pages = {}       # snapshot_id -> list[PageRec]

    def ingest(self, snapshot: Snapshot) -> None:
        self.pages[snapshot.snapshot_id] = [
            PageRec(p.url, snapshot.snapshot_id, snapshot.taken_on, p.markdown) for p in snapshot.pages
        ]
        self.snapshots.append((snapshot.taken_on, snapshot.snapshot_id))
        self.snapshots.sort()

    def _snapshot_for(self, as_of):
        """Newest snapshot taken on or before as_of; the oldest one if as_of predates them all."""
        if not self.snapshots:
            return None
        if not as_of:
            return self.snapshots[-1][1]
        i = bisect_right([d for d, _ in self.snapshots], as_of[:10])
        return self.snapshots[max(i - 1, 0)][1]

    def answer(self, question, as_of=None):
        sid = self._snapshot_for(as_of)
        if sid is None:
            return Answer(text="I don't know.", confident=False)
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

    def conflicts(self):
        return []


def build():
    return Knowledge()
