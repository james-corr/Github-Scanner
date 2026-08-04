#!/usr/bin/env python3
"""GitHub Repo Scanner.

Snapshots AI-relevant repos every run, diffs against the previous snapshot
to compute star velocity, and enriches the finalist repos (README, latest
release, subscriber/network counts) for a weekly newsletter feed.

Stdlib only. See scanner/README.md for the output contract and how to tune
scanner/queries.json.
"""

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DATA_DIR = REPO_ROOT / "data"
QUERIES_PATH = SCRIPT_DIR / "queries.json"

API_ROOT = "https://api.github.com"
USER_AGENT = "github-scanner/1.0 (+https://github.com/james-corr/Github-Scanner)"
README_MAX_CHARS = 1500
RELEASE_BODY_MAX_CHARS = 300


# ---------------------------------------------------------------------------
# Query plan
# ---------------------------------------------------------------------------

def load_queries():
    with QUERIES_PATH.open() as f:
        return json.load(f)


def resolve_date_placeholders(query, now):
    seven = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    thirty = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    return query.replace("{7d}", seven).replace("{30d}", thirty)


def build_query_plan(cfg, now):
    """Return [(lane_tag, query_string), ...] with dates substituted.

    lane_tag is one of "velocity:builder", "velocity:business",
    "new_this_week", "new_this_month".
    """
    plan = []
    for lane, qlist in cfg["velocity"].items():
        for q in qlist:
            plan.append((f"velocity:{lane}", resolve_date_placeholders(q, now)))
    for lane, qlist in cfg["new_repo_discovery"].items():
        for q in qlist:
            plan.append((lane, resolve_date_placeholders(q, now)))
    return plan


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

class GitHubClient:
    def __init__(self, token):
        self.token = token
        self.authenticated = bool(token)
        self.search_delay = 2.0 if self.authenticated else 7.0
        self.core_delay = 0.5 if self.authenticated else 1.5

    def _request(self, url):
        req = urllib.request.Request(url)
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Accept", "application/vnd.github+json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()
        except urllib.error.URLError as e:
            return None, {}, str(e).encode()

    def get_json(self, url, allow_retry=True):
        """Returns (data, status). data is None on any non-2xx response."""
        status, headers, body = self._request(url)
        if status is None:
            print(f"  network error fetching {url}: {body}", file=sys.stderr)
            return None, status
        if status == 403 and headers.get("X-RateLimit-Remaining") == "0" and allow_retry:
            reset = int(headers.get("X-RateLimit-Reset", "0"))
            sleep_for = max(reset - time.time(), 0) + 1
            print(f"  rate limited, sleeping {sleep_for:.0f}s until reset", file=sys.stderr)
            time.sleep(sleep_for)
            return self.get_json(url, allow_retry=False)
        if status >= 400:
            return None, status
        try:
            return json.loads(body), status
        except json.JSONDecodeError:
            return None, status

    def search_repositories(self, query):
        url = (
            f"{API_ROOT}/search/repositories?q={urllib.parse.quote(query)}"
            "&sort=stars&order=desc&per_page=100"
        )
        data, status = self.get_json(url)
        if status == 422:
            print(f"  422 on query, skipping: {query}", file=sys.stderr)
            return [], status
        if data is None:
            print(f"  search failed (status={status}) for query: {query}", file=sys.stderr)
            return [], status
        return data.get("items", []), status

    def repo_details(self, full_name):
        data, status = self.get_json(f"{API_ROOT}/repos/{full_name}")
        return data if status == 200 else None

    def repo_readme(self, full_name):
        data, status = self.get_json(f"{API_ROOT}/repos/{full_name}/readme")
        if status != 200 or not data or data.get("encoding") != "base64":
            return None
        try:
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        except (ValueError, TypeError):
            return None

    def repo_latest_release(self, full_name):
        data, status = self.get_json(f"{API_ROOT}/repos/{full_name}/releases?per_page=1")
        if status != 200 or not data:
            return None
        latest = data[0]
        body = (latest.get("body") or "").strip()
        return {
            "name": latest.get("name") or latest.get("tag_name"),
            "published_at": latest.get("published_at"),
            "body_excerpt": body[:RELEASE_BODY_MAX_CHARS],
        }


# ---------------------------------------------------------------------------
# Record normalization, lane classification, noise filtering
# ---------------------------------------------------------------------------

def normalize_record(item):
    owner = item.get("owner") or {}
    license_info = item.get("license") or {}
    return {
        "full_name": item["full_name"],
        "html_url": item["html_url"],
        "description": item.get("description") or "",
        "stars": item.get("stargazers_count", 0),
        "forks": item.get("forks_count", 0),
        "language": item.get("language"),
        "topics": item.get("topics", []),
        "created_at": item.get("created_at"),
        "pushed_at": item.get("pushed_at"),
        "owner_type": owner.get("type"),
        "license": license_info.get("spdx_id"),
        "homepage": item.get("homepage") or None,
        "open_issues": item.get("open_issues_count", 0),
        "archived": item.get("archived", False),
        "fork": item.get("fork", False),
    }


def derive_lane(query_lanes):
    velocity_lanes = {t.split(":", 1)[1] for t in query_lanes if t.startswith("velocity:")}
    if len(velocity_lanes) == 2:
        return "both"
    if len(velocity_lanes) == 1:
        return next(iter(velocity_lanes))
    return "new"


def _word_boundary_search(keyword, haystack):
    return re.search(rf"\b{re.escape(keyword)}\b", haystack) is not None


def is_ai_relevant(record, keywords):
    haystack = " ".join(
        [record["full_name"], record["description"], " ".join(record["topics"])]
    ).lower()
    return any(_word_boundary_search(kw, haystack) for kw in keywords)


def matches_noise_pattern(record, patterns):
    haystack = f'{record["full_name"]} {record["description"]}'.lower()
    for pattern in patterns:
        if _word_boundary_search(pattern, haystack):
            return pattern
    return None


def classify_and_filter(raw_by_name, cfg):
    """Split deduped raw records into (universe, filtered)."""
    universe = {}
    filtered = []
    for full_name, entry in raw_by_name.items():
        record = entry["record"]
        query_lanes = sorted(entry["lanes"])
        record["lane"] = derive_lane(query_lanes)
        record["query_lanes"] = query_lanes
        record["is_new_this_week"] = "new_this_week" in query_lanes
        record["is_new_this_month"] = "new_this_month" in query_lanes

        ai_relevant = is_ai_relevant(record, cfg["ai_relevance_keywords"])
        record["flags"] = ["ai_relevant"] if ai_relevant else []

        reason = None
        if record["archived"]:
            reason = "archived"
        elif record["fork"]:
            reason = "fork"
        elif not record["description"].strip():
            reason = "empty description"
        else:
            pattern = matches_noise_pattern(record, cfg["noise_name_patterns"])
            if pattern:
                reason = f"list-repo pattern: {pattern}"
        if reason is None and record["lane"] == "new" and not ai_relevant:
            reason = "not ai-relevant (discovery-only, no keyword match)"

        if reason:
            filtered.append({"full_name": full_name, "reason": reason})
        else:
            universe[full_name] = record
    return universe, filtered


# ---------------------------------------------------------------------------
# Velocity
# ---------------------------------------------------------------------------

def compute_velocity(universe, prior_universe, days_elapsed):
    """Attach velocity fields to universe records that exist in both snapshots."""
    for full_name, record in universe.items():
        prior = prior_universe.get(full_name)
        if prior is None or record["lane"] not in ("builder", "business", "both"):
            continue
        stars_then = prior.get("stars", 0)
        delta = record["stars"] - stars_then
        stars_per_day = delta / days_elapsed if days_elapsed else 0.0
        record["velocity"] = {
            "stars_then": stars_then,
            "delta": delta,
            "stars_per_day": round(stars_per_day, 3),
            "delta_7d": round(stars_per_day * 7, 1),
            "pct_growth": round(delta / stars_then, 4) if stars_then else None,
        }


def rank_movers_and_breakouts(universe, breakout_floor, top_n=10):
    movers = {"builder": [], "business": []}
    breakouts = {"builder": [], "business": []}
    for record in universe.values():
        if "velocity" not in record:
            continue
        lanes = ["builder", "business"] if record["lane"] == "both" else [record["lane"]]
        for lane in lanes:
            if lane not in movers:
                continue
            movers[lane].append(record)
            if record["velocity"]["stars_then"] >= breakout_floor and record["velocity"]["pct_growth"] is not None:
                breakouts[lane].append(record)
    for lane in movers:
        movers[lane] = sorted(movers[lane], key=lambda r: r["velocity"]["delta_7d"], reverse=True)[:top_n]
    for lane in breakouts:
        breakouts[lane] = sorted(breakouts[lane], key=lambda r: r["velocity"]["pct_growth"], reverse=True)[:top_n]
    return movers, breakouts


def new_repo_lists(universe, top_n=25):
    new_week = sorted(
        (r for r in universe.values() if r["is_new_this_week"]),
        key=lambda r: r["stars"], reverse=True,
    )[:top_n]
    new_month = sorted(
        (r for r in universe.values() if r["is_new_this_month"]),
        key=lambda r: r["stars"], reverse=True,
    )[:top_n]
    return new_week, new_month


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------

def strip_readme(raw_text):
    lines = raw_text.splitlines()
    kept = []
    badge_line_re = re.compile(r"^\s*(\[!\[.*?\]\(.*?\)\]\(.*?\)|\!\[.*?\]\(.*?\))\s*$")
    for line in lines:
        if badge_line_re.match(line):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        kept.append(line)
    text = "\n".join(kept)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:README_MAX_CHARS]


def collect_finalists(movers, breakouts, new_week, new_month):
    finalists = {}
    for group in (movers["builder"], movers["business"], breakouts["builder"], breakouts["business"], new_week, new_month):
        for record in group:
            finalists[record["full_name"]] = record
    return finalists


def enrich_finalists(client, finalists, no_enrich=False):
    for full_name, record in finalists.items():
        if no_enrich:
            record["enrichment"] = None
            continue
        details = client.repo_details(full_name)
        time.sleep(client.core_delay)
        readme_raw = client.repo_readme(full_name)
        time.sleep(client.core_delay)
        release = client.repo_latest_release(full_name)
        time.sleep(client.core_delay)

        record["enrichment"] = {
            "subscribers_count": details.get("subscribers_count") if details else None,
            "network_count": details.get("network_count") if details else None,
            "topics": details.get("topics") if details else record["topics"],
            "license": (details.get("license") or {}).get("spdx_id") if details else record["license"],
            "homepage": details.get("homepage") if details else record["homepage"],
            "readme_excerpt": strip_readme(readme_raw) if readme_raw else None,
            "latest_release": release,
        }


# ---------------------------------------------------------------------------
# Prior snapshot
# ---------------------------------------------------------------------------

def find_prior_snapshot(data_dir, today_str):
    candidates = sorted(
        p for p in data_dir.glob("*-repos.json")
        if p.name != f"{today_str}-repos.json" and p.name != "latest.json"
    )
    return candidates[-1] if candidates else None


def load_prior(path):
    with path.open() as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def build_universe_output(universe):
    """Universe entries carry search-level fields only, not velocity/enrichment."""
    drop = {"velocity", "enrichment"}
    return {
        name: {k: v for k, v in record.items() if k not in drop}
        for name, record in universe.items()
    }


def render_signals(report, baseline):
    lines = []
    n_movers = len(report["movers"]["builder"]) + len(report["movers"]["business"])
    n_breakouts = len(report["breakouts"]["builder"]) + len(report["breakouts"]["business"])
    lines.append(f"- New this week: {len(report['new_this_week'])}")
    lines.append(f"- New this month: {len(report['new_this_month'])}")
    if baseline:
        lines.append("- Velocity: baseline run, no prior snapshot to diff against yet.")
    else:
        lines.append(f"- Movers tracked: {n_movers}")
        lines.append(f"- Breakouts tracked: {n_breakouts}")
        top_mover = None
        top_breakout = None
        for lane in ("builder", "business"):
            for r in report["movers"][lane]:
                if top_mover is None or r["velocity"]["delta_7d"] > top_mover["velocity"]["delta_7d"]:
                    top_mover = r
            for r in report["breakouts"][lane]:
                if top_breakout is None or r["velocity"]["pct_growth"] > top_breakout["velocity"]["pct_growth"]:
                    top_breakout = r
        if top_mover:
            lines.append(f"- Biggest mover: {top_mover['full_name']} (+{top_mover['velocity']['delta_7d']}/wk)")
        if top_breakout:
            lines.append(f"- Biggest breakout: {top_breakout['full_name']} ({top_breakout['velocity']['pct_growth'] * 100:.0f}% growth)")
    return "\n".join(lines)


def render_entry_line(rank, record, with_velocity):
    topics = ", ".join(record["topics"][:5])
    base = (
        f"{rank}. [{record['full_name']}]({record['html_url']}) "
        f"— {record['stars']}★"
    )
    if with_velocity and "velocity" in record:
        v = record["velocity"]
        pct = f"{v['pct_growth'] * 100:.1f}%" if v["pct_growth"] is not None else "n/a"
        base += f" ({v['delta_7d']:+}/wk, {pct} growth)"
    base += (
        f" | {record['language'] or 'n/a'} | created {record['created_at']} "
        f"| last push {record['pushed_at']} | topics: {topics}\n"
        f"   {record['description']}"
    )
    return base


def render_markdown(payload):
    lines = [f"# GitHub Repo Scanner — {payload['generated_at']}", ""]
    lines.append("## Signals")
    lines.append(render_signals(payload["report"], payload["baseline"]))
    lines.append("")

    if payload["baseline"]:
        lines.append("_First run: no prior snapshot exists yet. Velocity sections populate next run._")
        lines.append("")
    else:
        for label, key in (("Movers", "movers"), ("Breakouts", "breakouts")):
            lines.append(f"## {label}")
            for lane in ("builder", "business"):
                lines.append(f"### {lane.title()}")
                records = payload["report"][key][lane]
                if not records:
                    lines.append("_none this run_")
                else:
                    for i, r in enumerate(records, 1):
                        lines.append(render_entry_line(i, r, with_velocity=True))
                lines.append("")

    for label, key in (("New this week", "new_this_week"), ("New this month", "new_this_month")):
        lines.append(f"## {label}")
        records = payload["report"][key]
        if not records:
            lines.append("_none this run_")
        else:
            for i, r in enumerate(records, 1):
                lines.append(render_entry_line(i, r, with_velocity=False))
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(args):
    cfg = load_queries()
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")

    plan = build_query_plan(cfg, now)
    if args.queries is not None:
        plan = plan[: args.queries]

    if args.dry_run:
        print(f"Resolved query plan ({len(plan)} queries):")
        for lane, query in plan:
            print(f"  [{lane}] {query}")
        return

    token = args.token
    client = GitHubClient(token)
    print(f"Authenticated: {client.authenticated}", file=sys.stderr)

    raw_by_name = {}
    queries_failed = []
    for i, (lane, query) in enumerate(plan):
        print(f"[{i + 1}/{len(plan)}] ({lane}) {query}", file=sys.stderr)
        items, status = client.search_repositories(query)
        if status == 422:
            queries_failed.append(query)
        for item in items:
            record = normalize_record(item)
            full_name = record["full_name"]
            if full_name not in raw_by_name:
                raw_by_name[full_name] = {"record": record, "lanes": set()}
            raw_by_name[full_name]["lanes"].add(lane)
        if i < len(plan) - 1:
            time.sleep(client.search_delay)

    universe, filtered = classify_and_filter(raw_by_name, cfg)
    print(f"Universe: {len(universe)} repos, filtered: {len(filtered)}", file=sys.stderr)

    data_dir = args.data_dir
    prior_path = find_prior_snapshot(data_dir, today_str)
    baseline = prior_path is None
    days_elapsed = None
    prior_snapshot_name = None

    if not baseline:
        prior = load_prior(prior_path)
        prior_snapshot_name = prior_path.name
        prior_generated_at = datetime.fromisoformat(prior["generated_at"].replace("Z", "+00:00"))
        days_elapsed = (now - prior_generated_at).total_seconds() / 86400
        compute_velocity(universe, prior.get("universe", {}), days_elapsed)

    movers, breakouts = rank_movers_and_breakouts(universe, cfg["breakout_stars_then_floor"])
    new_week, new_month = new_repo_lists(universe)

    finalists = collect_finalists(movers, breakouts, new_week, new_month)
    print(f"Enriching {len(finalists)} finalists...", file=sys.stderr)
    enrich_finalists(client, finalists, no_enrich=args.no_enrich)

    payload = {
        "generated_at": now.isoformat(),
        "baseline": baseline,
        "prior_snapshot": prior_snapshot_name,
        "days_elapsed": round(days_elapsed, 2) if days_elapsed is not None else None,
        "authenticated": client.authenticated,
        "queries_run": len(plan),
        "queries_failed": queries_failed,
        "report": {
            "movers": movers,
            "breakouts": breakouts,
            "new_this_week": new_week,
            "new_this_month": new_month,
        },
        "universe": build_universe_output(universe),
        "filtered": filtered,
    }

    data_dir.mkdir(parents=True, exist_ok=True)
    json_path = data_dir / f"{today_str}-repos.json"
    md_path = data_dir / f"{today_str}-repos.md"
    latest_json = data_dir / "latest.json"
    latest_md = data_dir / "latest.md"

    json_text = json.dumps(payload, indent=2)
    md_text = render_markdown(payload)

    json_path.write_text(json_text)
    md_path.write_text(md_text)
    latest_json.write_text(json_text)
    latest_md.write_text(md_text)

    print(f"Wrote {json_path}, {md_path}, and updated latest.json/latest.md", file=sys.stderr)


def parse_args():
    parser = argparse.ArgumentParser(description="GitHub Repo Scanner")
    parser.add_argument("--dry-run", action="store_true", help="Print resolved queries, no network calls")
    parser.add_argument("--queries", type=int, default=None, help="Limit to the first N queries")
    parser.add_argument("--no-enrich", action="store_true", help="Skip finalist enrichment calls")
    parser.add_argument("--token", default=None, help="GitHub token; defaults to $GITHUB_TOKEN")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR, help="Directory for snapshot output")
    args = parser.parse_args()
    if args.token is None:
        args.token = os.environ.get("GITHUB_TOKEN")
    return args


if __name__ == "__main__":
    run(parse_args())
