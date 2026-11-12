# NOTES — mysolution

A deterministic, rules-and-retrieval knowledge system over the five website snapshots.
**No API key is needed. No LLM is called.** It uses only the Python standard library.

## Running the eval

The snapshot data is **not in this repo, by design**: the website data is shared for the event only and must not be redistributed. `snapshots/` and `snapshots_zip/` are listed in `.gitignore`.

Before running, copy the kit's `snapshots_zip/` folder into the repo root:

```text
<repo root>/
  snapshots_zip/              <- copy from the hackathon kit (5 zip files)
    01_2026-05-12_baseline.zip
    ...
    05_2026-09-28_latest.zip
  mysolution/
  harness/
  ...
```

Then, from the repo root:

```bash
pip install -r requirements.txt          # no third-party packages; standard library only
python harness/run_eval.py --submission mysolution --eval eval/public_eval.jsonl
```

On the first run the scorer unpacks the zips into `snapshots/`. Without `snapshots_zip/`, the scorer stops with `FileNotFoundError: 'snapshots'`.

Optional checks:

```bash
python harness/run_eval.py --submission mysolution --eval tests/my_questions.jsonl
python harness/run_eval.py --submission mysolution --snapshots harness/selftest/snapshots \
    --eval harness/selftest/eval.jsonl --conflicts-gold harness/selftest/conflicts_gold.json
```

Loading all five snapshots takes about 10 s. Answers take under 0.5 s at the 95th percentile.

## Approach

**Ingest.** Every page of every snapshot is kept. Each unique page version (URL plus content hash) is parsed once into:
- **Facts** from every markdown rate table. Each fact records the product (from the table caption and nearby text), the row label, and the tiers: term, model-year range, balance or loan-amount range. It also records each measure in the row (rate, dividend rate, APY, APR, fee), the table's "Rates are effective as of" date, and the page. Short `label: value` lines such as `Replacement fee: $2.50` become facts too. Rows marked as expired ("offer ended 2025-09-30") are dropped.
- **Sentences** for keyword search, each tagged with its section heading and the page title.
- **Links**, branch **hours** blocks (Lobby and Drive-up schedules), the **branch locator** (addresses), **routing numbers**, and each article's **publication date**.

A snapshot's *view* is every page present in that snapshot, plus the newest copy of any page seen earlier but missing now. Carried-forward pages rank below live ones.

**Which snapshot.** With `as_of`, the newest snapshot taken on or before that date is used. A date written in the question ("as of May 2026") is treated the same way. Without a date, snapshot 05 is used.

## How "current" is decided

1. **Authority first.** Pages rank in this order:
   - product or rate page (`/personal/*`, `/business/*`);
   - rate sheet (`/rates`);
   - about and contact pages;
   - general pages;
   - promotion landing pages;
   - calculators;
   - blog, learn and newsroom pages;
   - legacy `info.` host pages.

   A stale table on a calculator or promotion page never beats the product page.
2. **Then the effective date.** If two equally trusted pages match equally well, the newer "Rates are effective as of" date wins. For example, an old copy of the auto table left on the checking page loses to the auto page's own table.
3. **Then recency of publication.** This applies to facts that only articles state, such as branch counts, awards and closures. Among near-equal matches, the most recently published article wins. Pages without a date that are live in the current view count as current.

## Answering

Questions are routed in this order:
1. **Routing number:** a vote across pages, weighted by authority.
2. **Link questions:**
   - For "link to see certificate rates", the product page that holds those rates, plus `#rates`.
   - Otherwise, the best-matching `dupaco.com` link. A subdomain that matches the action wins: "join online" gives `join.dupaco.com`.
3. **Hours:** the most common Lobby or Drive-up schedule, followed by a question asking which branch. "Do all branches have the same hours" gets two differing schedules. For a named branch, that branch's posted hours are used. If none are posted, the answer is a refusal.
4. **Branch address:** taken from the locator. A branch that isn't listed gets a refusal.
5. **Rates and fees:**
   - Facts are matched on product words. A row is excluded if its term, model year or balance or loan amount contradicts the question.
   - Business, jumbo, IRA, refinance and intro rows rank lower unless the question asks for them.
   - Rows come from **one** table on the most authoritative page:
     - an exact tier when the question names one;
     - all tiers of that one product when it doesn't;
     - one row for "highest" or "lowest";
     - personal products first, then business, for "what … do you offer".
   - Certificate list answers add a second line with the current specials from the same page and snapshot.
   - **Product families.** Each table is assigned one family (certificate, savings, checking, money market, HSA, IRA, auto, mortgage, home equity, card, …). The evidence is used in this order: the table's caption and row labels; then structure (a deposit table whose rows are terms is a certificate table); then the section caption and heading; then the page; then nearby text. A deposit/loan guard keeps APR tables out of the deposit families. A question that names a family uses only tables of that family. A copy of a table on a `/business` page ranks below the personal page's copy unless the question says "business".
6. **Change questions:** this applies only when the question explicitly asks about change or history ("change", "changed", "previously", "used to", "before", "went up/down", "increase/decrease").
   - The same row on the same page is followed snapshot by snapshot, and the answer is given as `old value (from date) → new value (from date)`.
   - Rows relabelled between crawls are still followed (for example "2023–26 Model Year" becoming "2024-27 Model Year"). The match uses the same page, term and tier position.
   - All other questions return only the current value.
7. **Eligibility and requirements questions:** these are answered from the best whole *section*, meaning a heading plus its bullets. The heading must be about eligibility or requirements, and the section must cover the question's topic. If the answer mentions the "charter area", the site's service-area sentence ("serves residents in N counties throughout Iowa, … Wisconsin", from the newest dated page) is appended with its source.
   - **Phone and contact questions** ("who do I call", "what number", "how do I report", "what should I do if …") are answered from a sentence that contains a phone number *and* the question's topic. Numbers are shown in `800-373-7600` form.
8. **Everything else:** sentence search.
   - Scoring uses IDF, page authority, page-title context and a boost for FAQ questions.
   - For "how many X", only sentences that state a number before X are kept, and dated pages are preferred.
   - The best sentence is joined with the neighbouring sentence that covers the question words it is missing. When the section heading names what was asked, it is added as a prefix.

Answers stay short. They never list values from other pages or older snapshots. Any disagreement goes in `conflict_note`, not in the answer text. Answers are cached per (normalised question, snapshot), so asking twice gives the same answer.

## Refusal logic

The system answers `confident=False`, with text that contains **no digits**, when:
- a rate is asked for a product with no published rate fact and no prose sentence quoting such a rate (for example a 7/1 ARM, a construction loan, or a special that didn't exist on the `as_of` date);
- a named branch has no posted hours or address;
- the best search match covers less than 45% of the question's weighted words.

## Conflict rules (`conflicts()`)

Conflicts are reported for the latest snapshot:
- **Stale copies.** A lower-authority page states a different value for the same product, term, model-year tier position, loan-amount tier and measure as a product or rate page. The resolution is the product page's value. The reason names the stale page type and the effective date.
- **Changed between snapshots.** A value on the same product or rate page, for the same row and measure, differs from an earlier snapshot. The resolution is the newest value.
- **Contradictory dated claims.** Different branch counts, or different Forbes recognition counts, across pages. The resolution is the most recently published page.

Different tiers are never reported as conflicts. The same change shown in several tables (personal, business, IRA) is reported once. Formatting artifacts ("2.90%" vs "2.90%–12.50%") are skipped.

## Testing

- `tests/my_questions.jsonl`: 36 of my own questions. They include rewordings, `as_of` dates, other rates, fees and branches, and refusals. Expected values were checked against the raw snapshot pages.
- `harness/selftest`: the kit's tiny selftest, including `--conflicts-gold`. It scores 1.0.
