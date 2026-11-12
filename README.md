# Dupaco knowledge system

A knowledge system that answers credit-union members' questions from five dated crawls of the Dupaco website. It gives the current, correctly tiered value with a source, and says "I don't know" when the site doesn't publish an answer. It uses rules and retrieval only: the Python standard library, no LLM.

## Results

| | |
|---|---|
| Public eval (40 questions) | **0.921** |
| Stale or wrong values in answers | **0** |
| Answer latency (95th percentile) | **~0.34 s** |
| Kit selftest with conflicts | 1.0 |
| Own test set (`tests/my_questions.jsonl`, 52 questions) | 1.0 |

## How to run

The website data isn't in this repo, by design: it was shared for the event only. Copy the kit's `snapshots_zip/` folder (5 zip files) into the repo root, then run:

```bash
pip install -r requirements.txt     # empty: standard library only
python harness/run_eval.py --submission mysolution --eval eval/public_eval.jsonl
```

**No API key is needed.** The code never reads one.

## Layout

- `mysolution/solution.py`: the solution (`build()` → `ingest` / `answer` / `conflicts`)
- [`NOTES.md`](NOTES.md): the full approach, including how "current" is decided, conflict and refusal rules, and known limitations
- `tests/my_questions.jsonl`: 52 extra questions, checked by hand against the raw snapshots
- `harness/`, `eval/`, `starter/`, [`KIT_README.md`](KIT_README.md): the original hackathon kit, unchanged
