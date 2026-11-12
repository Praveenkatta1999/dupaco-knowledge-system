# NOTES — mysolution

A deterministic, rules-and-retrieval knowledge system over the five website snapshots. It uses only the Python standard library. **No API key is needed and no LLM is called.** The code never reads `ANTHROPIC_API_KEY` or any other environment variable.

Public eval: **0.921**, with no stale-value trap hits. Loading the snapshots takes about 10 s, and 95% of answers come back within about 0.35 s.

## Running the eval

The snapshot data is **not in this repo, by design**: the website data is shared for the event only. `snapshots/` and `snapshots_zip/` are git-ignored. Copy the kit's `snapshots_zip/` folder (5 zip files) into the repo root, then run:

```bash
pip install -r requirements.txt     # empty: standard library only
python harness/run_eval.py --submission mysolution --eval eval/public_eval.jsonl
```

The scorer unpacks the zips into `snapshots/` on its first run. Without them it stops with `FileNotFoundError: 'snapshots'`. This was tested from a fresh clone in a clean Python 3.11 venv, with no API key set.

## Approach

**Ingest.** Every page of every snapshot is kept, and each unique page version is parsed once into:
- **rate and fee facts** from markdown tables and short `label: value` lines;
- **sentences** for search, each tagged with its section heading and the page title;
- links, branch-hours blocks, the branch locator, routing numbers, and article publication dates.

Each fact records:
- the product and the row label;
- its tiers: term, model-year range, balance or loan-amount range;
- each measure: rate, dividend rate, APY, APR or fee;
- the table's "Rates are effective as of" date;
- a **product family**.

Expired offers ("offer ended …") are dropped.

A snapshot's view is its own pages plus the newest copy of any page seen earlier but now missing. Those carried-forward pages rank lower than live ones.

**Which snapshot.** With `as_of`, or a date in the question such as "as of May 2026", the newest snapshot on or before that date is used. Otherwise the latest snapshot is used.

**Product families.** Each table gets one family: certificate, savings, checking, money market, HSA, IRA, auto, mortgage, home equity, card, and so on. The evidence is used in this order:
1. caption and row labels;
2. structure: a deposit table whose rows are terms is a certificate table;
3. section caption and heading;
4. the page;
5. nearby text.

APR tables can never become deposit families. A question that names a family only uses tables of that family.

## How "current" is decided

1. **Authority.** Pages rank in this order:
   1. product page (`/personal/*`, `/business/*`)
   2. rate sheet (`/rates`)
   3. about and contact pages
   4. general pages
   5. promotion pages
   6. calculators
   7. blog, learn and newsroom pages
   8. legacy `info.` pages

   Tag and author index pages aren't indexed. A `/business` page's copy of a table ranks below the personal page's copy unless the question says "business".
2. **Effective date.** Between equally trusted, equally matching pages, the newer "Rates are effective as of" date wins.
3. **Publication date.** For facts that only articles state (branch counts, awards, closures), the most recently published article wins.

Answers quote values from **one** table on the winning page. Disagreeing values are never listed in the answer; they go in `conflict_note`.

## Answering

Questions are routed in this order:
1. **Routing number:** a vote across pages, weighted by authority.
2. **Links:** a product page plus `#rates` for "link to … rates". Otherwise the best `dupaco.com` link; a matching subdomain wins (`join.dupaco.com` for "join online").
3. **Hours:** the most common Lobby or Drive-up schedule, then "which branch?". Asked whether all branches are the same, it gives two schedules. A named branch gets its own posted hours.
4. **Branch address:** from the locator.
5. **Change questions:** only when the question explicitly asks about change ("change", "previously", "used to", "before", "went up/down", "increase/decrease"). The same row is followed through the snapshots: `old (from date) → new (from date)`. This still works when a tier was relabelled between crawls.
6. **Rates and fees:**
   - an exact tier, or all tiers of one product if no tier is named;
   - one row for "highest" or "lowest";
   - personal then business products for "what … do you offer";
   - certificate lists add a line with the current specials.
7. **Search:**
   - Phone and contact questions use a sentence that has a phone number and the question's topic.
   - Eligibility and requirements questions use the best whole section, i.e. a heading plus its bullets. If that answer mentions the "charter area", the site's service-area sentence (counties and states) is added.
   - Otherwise, sentence search:
     - The best sentence is joined with any neighbouring sentence that covers question words it is missing, or that continues it.
     - For "how do I / who / what is" questions, legal pages and PDFs weigh less and member-facing pages weigh more.
     - "How many" questions only use sentences that state a number, and prefer dated pages.

Answers are cached per (question, snapshot), so they're deterministic.

## Refusal logic

The system returns `confident=False`, with text that contains no digits, when:
- a rate is asked for a product that has no published rate (for example a 7/1 ARM, a construction loan, or a special that didn't exist on the `as_of` date);
- a named branch has no posted hours or address;
- the best search match covers less than 45% of the question.

## Conflicts (`conflicts()`)

Conflicts are reported for the latest snapshot. About 35 are reported on this data. Each one gets a topic, its values, sources, a resolution and a reason.
- **Stale copy:** a lower-authority page (calculator, promotion, blog) shows a different value for the same product, term, tier and measure as the product page. The product page's value wins.
- **Changed between snapshots:** the same row on the same product page changed value. The newest value wins.
- **Contradictory dated claims:** branch counts and Forbes recognition counts. The most recently published page wins.

Different tiers are never reported as conflicts. The same change shown in personal, business and IRA tables is reported once.

## Testing

- `tests/my_questions.jsonl` has 52 of my own questions, with expected values checked by hand against the raw snapshot pages. They cover:
  - rewordings of public questions;
  - `as_of` dates;
  - other rates, fees and branches;
  - refusals;
  - change questions;
  - eligibility;
  - phone and contact questions;
  - dated list questions that must not mix product families.

  Score: **1.0**. Run it with `--eval tests/my_questions.jsonl`.
- `harness/selftest`, run with `--conflicts-gold`, scores **1.0** (conflict F1 1.0).

## Known limitations

These are the public questions that still score below 1.0:

| Question | Score | Why it falls short |
|---|---|---|
| dup-064, dup-066 (lobby and drive-up hours) | 0.85 | The hours are correct, but they come from branch-opening press releases. The expected source pages (`/`, `/contact`) contain no hours in any snapshot. |
| dup-005 (certificate rates) | 0 locally | The eval item lists no required values. Our answer lists every term plus the specials. |
| dup-060 (update contact info) | 0 | The only grounded answer is in the privacy policy. No general rule picks it out over better-matching sentences. |
| dup-088 (volunteer time off) | 0 | We give the newest figure with its year (256 in 2024). The expected answer is the older 2023 figure (294). |

Every question in my own test set passes. One gap is known: a question like "When is the drive-up open?", which never says "hours", is not routed to the hours lookup. I left it that way on purpose, because widening the trigger to "open/close" would capture branch-closing questions that currently pass.
