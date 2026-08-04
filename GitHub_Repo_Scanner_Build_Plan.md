---
type: plan
title: GitHub Repo Scanner Build Plan
status: approved, not yet built
created: 2026-08-01
tags: [work, content, ai, build]
links: ["[[GitHub_Repo_Scanner]]", "[[AI_Newsletter_Agent]]", "[[Personal_Brand]]"]
---
# GitHub Repo Scanner: Build Plan

Last edited on: 08/01/26

Approved 08/01/26. Supersedes the Spec section in [[GitHub_Repo_Scanner]]. Not yet built.

## Context

The goal is a weekly feed of the AI repos actually gaining traction, to aggregate into a Substack
newsletter and pull from sporadically for LinkedIn and short-form video. Every entry needs a URL
and enough factual substance to argue why it matters, ideally to a business audience rather than
only to engineers.

Two things in the original [[GitHub_Repo_Scanner]] note do not serve that goal and are being
replaced.

**The signal was wrong.** "Top repos created in the last 7 days, sorted by stars" finds *new*
repos, not *in-demand* ones. That query reliably returns awesome-lists, course dumps, and
launch-day spikes, and it structurally cannot surface a repo that has existed six months and is
quietly being adopted. Adoption is the story a business audience cares about. The GitHub search
API exposes no star-velocity field, so we snapshot star counts every run and diff against the
previous run. Real week-over-week velocity, at the cost of one warm-up week.

**The "only hit `/search/repositories`" constraint was written to dodge rate limits that no longer
apply.** Running on GitHub Actions provides an automatic `GITHUB_TOKEN` worth 1,000 requests/hour.
Keeping the constraint would cap every entry at name, description, topics, and stars, which is too
thin to write an honest business-impact argument from. We drop it and enrich the finalists.

Decisions made:
- Run **both** signals: velocity (snapshot diff) and new-repo discovery. Week one produces the
  new-repo lanes and lays the baseline; velocity populates from week two.
- Cover **both** lanes as separate sections: builder tooling and business-applicable tooling.
- Run on **GitHub Actions inside `james-corr/ai-news-agent`**, public repo, so the scanner and the
  newsletter agent share one deploy.
- **Enrich the finalists only**: search API for the full universe, then full metadata plus README
  and latest release for the roughly 20 repos that make the ranked lists.
- The script stays deterministic and does no editorializing. The "why this matters" writeup is
  [[AI_Newsletter_Agent]]'s job, reading the published data file.

## Prerequisites

Verified on this machine 08/01/26: `python3` at `/opt/homebrew/bin/python3`. No `uv`, no `gh`, no
`GITHUB_TOKEN`, git has no global `user.name` or `user.email`, and there is no local clone anywhere
under `~/Desktop/Projects/`.

`https://api.github.com/repos/james-corr/ai-news-agent` returns 404 while the `james-corr` user
returns 200. A 404 means the repo is either absent or private, and that cannot be distinguished
without auth. Step 1 resolves it before anything gets created. Note that [[AI_Newsletter_Agent]]
currently claims the repo exists; that claim is unverified and may need correcting.

Setup steps, in order:

1. **Confirm the repo state.** Check github.com/james-corr?tab=repositories while signed in. If
   `ai-news-agent` exists as a private repo, flip it public and clone it rather than creating a
   duplicate.
2. **Install `gh`**: `brew install gh`, then `gh auth login`. This also gives git a credential
   helper, so no PAT juggling for pushes.
3. **Set git identity**: `git config --global user.name` and `user.email`.
4. **Create or clone the repo** into `~/Desktop/Projects/ai-news-agent`. If creating:
   `gh repo create james-corr/ai-news-agent --public --clone`.

## Repo layout

```
ai-news-agent/
  .github/workflows/scan.yml     # weekly cron + manual dispatch
  scanner/
    scan.py                      # the whole scanner, stdlib only
    queries.json                 # tunable query set, edited without touching code
    README.md                    # what it does, how to tune, output contract
  data/
    YYYY-MM-DD-repos.json        # dated artifact, one per run
    YYYY-MM-DD-repos.md          # human-scannable version
    latest.json                  # copy of newest run, stable URL
    latest.md
  README.md
```

`latest.json` matters: it gives the newsletter agent a fixed raw URL
(`raw.githubusercontent.com/james-corr/ai-news-agent/main/data/latest.json`) instead of having to
guess a date. Public repo means no auth needed to read it.

The dated JSON files **are** the snapshots. Each run reads the most recent prior `*-repos.json`
from the checked-out `data/` directory and diffs against its `universe` block. No separate snapshot
store, and git history gives the time series for free.

Python is stdlib only (`urllib`, `json`, `base64`, `datetime`) so the workflow needs no dependency
install step and cannot break on a transitive upgrade months from now.

## Scanner design

### 1. Query set (`scanner/queries.json`)

Two groups of GitHub search qualifier strings. All calls are
`GET /search/repositories?q=...&sort=stars&order=desc&per_page=100`.

**Velocity universe** (broad; defines which repos get tracked week over week). One query per topic,
since GitHub treats multiple `topic:` qualifiers as AND, not OR:

- builder: `topic:llm stars:>300 pushed:>{30d}`, same shape for `ai-agents`, `mcp`, `rag`,
  `llmops`, `agentic-ai`, `prompt-engineering`
- business: `topic:automation stars:>300 pushed:>{30d}`, same for `workflow-automation`,
  `self-hosted`, `chatbot`, `ai-assistant`, `document-ai`

**New-repo discovery** (no topic filter, since brand-new repos usually have none set):

- `created:>{7d} stars:>50`
- `created:>{30d} stars:>200`

Roughly 15 queries, one page each. `{7d}` and `{30d}` are substituted at runtime with
`YYYY-MM-DD`. Keeping these in JSON means tuning over the first few weeks is a data edit, not a
code change.

### 2. Fetch and rate limiting

- Read `GITHUB_TOKEN` from the environment. Actions injects it; locally, export a no-scope PAT or
  run unauthenticated for testing.
- Sleep between search calls based on token presence (2s authenticated, 7s not). Search is capped
  at 30 requests/minute authenticated regardless of the 1,000/hour core budget, so search and
  enrichment get separate pacing.
- On HTTP 403 with `X-RateLimit-Remaining: 0`, sleep until `X-RateLimit-Reset` and retry once.
- On 422 (bad query), log the offending query and continue. One malformed query should not cost the
  week's snapshot.
- Set an explicit `User-Agent`. GitHub rejects requests without one.

### 3. Classification and noise filtering

Every result normalizes to one record keyed by `full_name`: `full_name`, `html_url`,
`description`, `stars`, `forks`, `language`, `topics`, `created_at`, `pushed_at`, `owner_type`,
`license`, `homepage`, `open_issues`, `lane`, `flags`.

- **Lane** comes from which query group returned it. A repo returned by both is tagged `both`, not
  duplicated.
- **AI relevance** reuses the keyword list already agreed in the original spec (ai, llm, gpt,
  claude, agent, machine-learning, deep-learning, generative, copilot, assistant, automation,
  dev-tool, developer-tool, cli, mcp) matched against `full_name`, `description`, and `topics`.
  This only matters for the new-repo lanes, which are not topic-filtered.
- **Noise** is flagged, not silently dropped. Excluded records move to a `filtered` array with a
  reason, so nothing disappears without a trace. Rules: `archived` or `fork` is true; empty
  description; or name/description matching list-repo patterns (`awesome`, `roadmap`, `interview`,
  `cheatsheet`, `100-days`, `learn-`, `-course`, `tutorial`, `books`, `free-`, `resources`,
  `list-of`).

### 4. Velocity computation

For each repo present in both this run's universe and the prior snapshot's:

- `delta = stars_now - stars_then`
- `days_elapsed` from the two snapshot timestamps, so an off-schedule run does not distort the
  figure
- `stars_per_day = delta / days_elapsed`, and `delta_7d = stars_per_day * 7` as the comparable
  headline number
- `pct_growth = delta / stars_then`

Two ranked lists, because raw delta and percentage tell different stories. 500 new stars on a 100k
repo is noise; 500 on a 2k repo is a breakout.

- **Movers**: ranked by `delta_7d`, top 10, split by lane.
- **Breakouts**: ranked by `pct_growth`, top 10, floor of `stars_then >= 200` so tiny repos do not
  dominate on a low base.

If no prior snapshot exists, the report sets `"baseline": true` and both velocity sections say
plainly that this is the first run rather than rendering empty tables.

### 5. Finalist enrichment

After ranking, take the union of every repo appearing in any ranked list (roughly 20, deduped) and
pull:

- `GET /repos/{owner}/{repo}` for `subscribers_count`, `network_count`, full `topics`, `license`,
  `homepage`
- `GET /repos/{owner}/{repo}/readme`, base64-decode, strip badge lines and HTML, keep the first
  ~1,500 characters. This is what actually tells a newsletter agent what the project *does*.
- `GET /repos/{owner}/{repo}/releases?per_page=1` for latest release name, date, and a body
  excerpt. Recent releases signal a maintained project, which matters for a business-adoption
  argument.

Three calls per finalist, about 60 total, against a 1,000/hour budget. Enrichment failures are
non-fatal: a missing README or a repo with no releases records `null` and the run continues.

### 6. Output

**JSON** is the primary artifact and the contract for the newsletter agent:

```json
{
  "generated_at": "ISO-8601",
  "baseline": false,
  "prior_snapshot": "2026-07-27-repos.json",
  "days_elapsed": 7.0,
  "authenticated": true,
  "queries_run": 15,
  "queries_failed": [],
  "report": {
    "movers":    {"builder": [], "business": []},
    "breakouts": {"builder": [], "business": []},
    "new_this_week":  [],
    "new_this_month": []
  },
  "universe": {"owner/repo": {"stars": 1234}},
  "filtered": [{"full_name": "owner/repo", "reason": "list-repo pattern: awesome"}]
}
```

Ranked entries carry the enrichment block; `universe` entries carry only the search-level fields.
`universe` holds every deduped repo fetched (expect 400 to 800), not just the ranked picks, so next
week's diff has coverage beyond whatever made this week's top 10.

**Markdown** mirrors the report for scanning. Each entry: rank, linked `full_name`, stars, stars
gained (7d normalized), % growth, language, created date, last push, top 5 topics, description,
URL. A short factual `## Signals` block at the top gives counts, the single biggest mover, and the
single biggest breakout. No editorial framing anywhere. Putting a weak version of the "why it
matters" argument in the script would only mislead the step whose job it is.

## Workflow (`.github/workflows/scan.yml`)

```yaml
on:
  schedule:
    - cron: '0 13 * * 1'      # Mondays 13:00 UTC, adjust to local timezone
  workflow_dispatch:           # manual trigger, needed for testing
permissions:
  contents: write              # required to commit results back
```

Steps: `actions/checkout@v4`, `actions/setup-python@v5` (3.12), run `python scanner/scan.py`, then
commit and push `data/` using the `github-actions[bot]` identity. `GITHUB_TOKEN` passed as env via
`${{ secrets.GITHUB_TOKEN }}`. No `pip install` step, since the script is stdlib only.

Two Actions behaviors worth knowing up front:

- Commits made with `GITHUB_TOKEN` do not trigger further workflow runs. That is what we want here:
  no loop risk.
- GitHub disables scheduled workflows after 60 days without repository activity, and bot commits
  may not reset that clock. GitHub emails a warning first. Mitigation is either an occasional
  manual `workflow_dispatch` or a real commit. Worth noting in the repo README so a silent stall
  does not go unnoticed.

## Verification

1. **Local dry run**: `python3 scanner/scan.py --dry-run` prints resolved query strings with dates
   substituted and makes no network calls.
2. **Local subset**: `python3 scanner/scan.py --queries 2 --no-enrich` end to end. Confirm the JSON
   parses, `universe` populates, and `filtered` catches at least one obvious list-repo.
3. **Local baseline**: full run unauthenticated. Confirm `baseline: true`, both new-repo lanes
   populate, velocity sections state the baseline condition rather than rendering empty, and the
   run stays inside the unauthenticated limit.
4. **Enrichment**: run with a local PAT exported and confirm README excerpts are decoded,
   badge-stripped, and truncated, and that a repo with no releases records `null` without failing
   the run.
5. **Diff path, without waiting a week**: copy the baseline JSON to a backdated filename, subtract
   known amounts from a handful of `universe` star counts, rerun, and confirm `delta_7d` and
   `pct_growth` match the seeded numbers and `days_elapsed` reflects the backdated timestamp.
6. **Rate-limit path**: run twice back to back unauthenticated to trip the 403, and confirm the
   script sleeps to reset and recovers instead of crashing.
7. **Read it as you would on a Monday.** If the top 10 is still mostly list repos and course dumps,
   tune the exclusion patterns and `stars:>` floors in `queries.json` before scheduling anything.
8. **Actions run**: push, trigger via `workflow_dispatch`, and confirm the job commits `data/`
   back, that `latest.json` updates, and that the raw URL resolves publicly.
9. **Enable the schedule last**, only after step 8 passes cleanly.

## Follow-ups after the build

- Update [[GitHub_Repo_Scanner]]: replace the Spec section with what was actually built, and record
  the `latest.json` raw URL as the documented input for the newsletter agent.
- Correct the "repo exists" claim in [[AI_Newsletter_Agent]] if it turns out it never did.
- Run `/log` to record the change in `z_meta/log.md`.
- Open question for a later session, not this build: whether the newsletter agent reads
  `latest.json` on its own schedule or the scan workflow triggers it directly. Keeping them
  decoupled by URL is the cleaner default.
