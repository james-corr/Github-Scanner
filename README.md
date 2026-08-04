# Github Scanner

A weekly feed of AI repos that are actually gaining traction on GitHub, split into builder
tooling and business-applicable tooling. Feeds a newsletter and gets pulled from for LinkedIn and
short-form content.

Runs every Monday via GitHub Actions (`.github/workflows/scan.yml`), snapshotting star counts and
diffing against the previous week to find real week-over-week velocity, not just newly created
repos. See `scanner/README.md` for how the scanner works and how to tune it.

## Layout

```
scanner/
  scan.py         # the scanner, stdlib only
  queries.json    # tunable query set and filters
  README.md       # scanner details, output contract
data/
  YYYY-MM-DD-repos.json / .md   # dated snapshots, one per run
  latest.json / latest.md       # stable pointer to the newest run
```

`data/latest.json` is the input other tools should read:
`https://raw.githubusercontent.com/james-corr/Github-Scanner/main/data/latest.json`

## Keeping the schedule alive

GitHub disables scheduled workflows after 60 days with no repository activity, and bot commits
made with `GITHUB_TOKEN` may not reset that clock. If the Monday runs stop showing up, check
Actions is still enabled and manually trigger `workflow_dispatch` once.
