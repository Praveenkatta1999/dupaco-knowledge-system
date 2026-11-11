# CLAUDE.md

Hackathon kit: build `mysolution/solution.py` (contract in `harness/contract.py`).

## Rules
- Never edit `harness/` or `eval/`.
- Never hardcode answers to the practice questions. Answers must come from the snapshots.
- After every change, run the scorer and report the score:
  `python harness/run_eval.py --submission mysolution --eval eval/public_eval.jsonl`
- Keep answers short and specific. Don't list every number: one stale or wrong value zeroes the question.
- `snapshots/`, `snapshots_zip/` and `.env` must stay out of git. Check `.gitignore` before committing.
- The solution must run with no API key. An LLM is optional and only used when `ANTHROPIC_API_KEY` is set.
