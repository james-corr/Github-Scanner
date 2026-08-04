# Scanner

`scan.py` finds AI-relevant GitHub repos that are actually gaining traction (not just newly
created), diffs against last run's star counts to compute velocity, and enriches the finalists
with README and release data. Stdlib only, no dependencies to install.

## Running it

```
python3 scan.py --dry-run              # print resolved queries, no network calls
python3 scan.py --queries 2 --no-enrich  # quick subset test
python3 scan.py                        # full run
```

Reads `GITHUB_TOKEN` from the environment (or pass `--token`). Unauthenticated works but is much
slower and more likely to hit rate limits — fine for a quick local check, not for a real run.

## Tuning (`queries.json`)

Edit this file, not the script, to tune results:

- `velocity.builder` / `velocity.business`: GitHub search qualifier strings, one per topic. These
  define the tracked universe. `{30d}` gets substituted with a real date at runtime.
- `new_repo_discovery.new_this_week` / `new_this_month`: no topic filter, since brand-new repos
  usually have no topics set yet.
- `ai_relevance_keywords`: only applied to discovery-lane repos (velocity repos are already
  on-topic via the `topic:` qualifier). Matched as whole words, not substrings.
- `noise_name_patterns`: name/description patterns that mark a repo as a list-repo, course dump,
  or similar (`awesome`, `tutorial`, `roadmap`, etc). Also whole-word matched.
- `breakout_stars_then_floor`: minimum prior star count for a repo to qualify for the breakouts
  (percent growth) ranking, so a 10-star repo doubling to 20 doesn't dominate.

If a Monday's output is still mostly list repos or off-topic noise, this file is where to fix it.

## Output contract

Every run writes `data/YYYY-MM-DD-repos.json` and `.md`, plus overwrites `data/latest.json` and
`data/latest.md`. `latest.json` is the stable input for downstream tools:

```
https://raw.githubusercontent.com/james-corr/Github-Scanner/main/data/latest.json
```

Top-level JSON fields:

- `baseline`: true on the first run (no prior snapshot), or if no prior snapshot could be found.
- `prior_snapshot` / `days_elapsed`: which file this run diffed against, and the real elapsed
  time (not assumed to be exactly 7 days).
- `report.movers` / `report.breakouts`: top 10 per lane (`builder`, `business`), ranked by
  7-day-normalized star delta and percent growth respectively. Empty until the second run.
- `report.new_this_week` / `report.new_this_month`: newly created repos passing the AI-relevance
  and noise filters.
- `universe`: every deduped repo this run fetched (400-800 typically), search-level fields only.
  This is what next week's run diffs against — don't delete old dated files, since only the most
  recent one is read but git history is the time series.
- `filtered`: repos excluded from `universe`, each with a `reason`. Nothing is silently dropped.

Ranked entries (movers/breakouts/new_this_week/new_this_month) carry an `enrichment` block:
`subscribers_count`, `network_count`, `readme_excerpt` (badge/HTML stripped, ~1500 chars),
`latest_release`. `universe` entries don't carry this — only the ~20-40 finalists get the extra
API calls.

The script does no editorializing. Writing the "why this matters" argument from this data is a
separate concern.
