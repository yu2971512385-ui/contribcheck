#!/usr/bin/env python3
"""Look a repository over before spending an evening on a pull request.

Three questions, asked in the order that saves the most time:

  1. Does this project accept the kind of contribution I am about to make?
     Several now have an AI policy, a CLA, or a rule about opening an issue
     first, and they are in files nobody reads until a PR is closed.
  2. Do pull requests from outside actually get merged here, and how fast?
     A repository with four hundred open PRs and three merges a quarter is
     telling you something.
  3. Has somebody already reported this?  The usual mistake is searching
     issues and forgetting that the fix may already be sitting in a PR.

Reads the GitHub API, with `gh auth token` if the CLI is logged in and
anonymously otherwise.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import re
import os
import shutil
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

__version__ = "0.1.0"

API = "https://api.github.com"

#: Files that tend to carry the rules, in the order a contributor meets them.
POLICY_FILES = (
    "CONTRIBUTING.md",
    ".github/CONTRIBUTING.md",
    "CONTRIBUTING.rst",
    "AGENTS.md",
    "AI_POLICY.md",
    ".github/AI_POLICY.md",
    "CLAUDE.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
    "CODE_OF_CONDUCT.md",
)

#: What to look for, and what it means if found. Ordered most serious first.
POLICY_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (r"\b(do not|don'?t|never|not)\b[^.\n]{0,40}\b(accept|allow|permit)\b[^.\n]{0,40}\b(ai|llm|generated)\b",
     "ai", "appears to refuse AI-assisted contributions"),
    (r"\bautonomous agents? (are|is) not allowed\b|\bno autonomous\b",
     "ai", "refuses autonomous agents"),
    (r"\b(ai|llm)[- ]generated\b[^.\n]{0,60}\b(not|won'?t|refus|reject|clos)\w*",
     "ai", "appears to reject AI-generated contributions"),
    (r"\b(must|should|required to|please)\b[^.\n]{0,40}\bdisclos\w+",
     "ai", "requires AI assistance to be disclosed"),
    (r"\b(ai|llm|language model|coding assistant|copilot|chatgpt|claude)\b",
     "ai", "mentions AI assistance"),
    (r"\b(cla|contributor licen[cs]e agreement|dco|sign(ed)?-off-by)\b",
     "legal", "asks for a CLA or a sign-off"),
    (r"\b(open|file|raise|create)\b[^.\n]{0,30}\bissue\b[^.\n]{0,40}\b(first|before)\b",
     "process", "asks for an issue before a pull request"),
    (r"\bconventional commits?\b", "process", "uses conventional commits"),
    (r"\bchangeset\b", "process", "wants a changeset"),
)

BOT_AUTHORS = ("app/dependabot", "app/renovate", "app/github-actions", "app/pre-commit-ci")


def _message_of(error: urllib.error.HTTPError) -> str:
    """GitHub puts the useful part in the body, not the status line."""
    try:
        return json.loads(error.read()).get("message", str(error))
    except (ValueError, OSError):
        return str(error)


class GitHub:
    def __init__(self, token: str | None) -> None:
        self.token = token

    def _request(self, url: str) -> object:
        request = urllib.request.Request(url)
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("User-Agent", f"contribcheck/{__version__}")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())

    def get(self, path: str, **params: str) -> object | None:
        url = f"{API}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        # The search endpoint has a secondary limit that trips on bursts well
        # before the hourly one runs out, and it clears in seconds, so backing
        # off beats telling the user to go and get a token they already have.
        for attempt in range(4):
            try:
                return self._request(url)
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    return None
                if error.code not in (403, 429):
                    raise
                detail = _message_of(error)
                retry_after = error.headers.get("Retry-After")
                if "secondary rate limit" in detail.lower() or retry_after:
                    time.sleep(float(retry_after) if retry_after else 2 ** attempt)
                    continue
                raise SystemExit(
                    f"github refused the request: {detail}\n"
                    "if you are not logged in, run `gh auth login` or set GITHUB_TOKEN"
                ) from error
            except urllib.error.URLError as error:
                raise SystemExit(f"could not reach github: {error.reason}") from error
            except OSError as error:
                # A connection dropped mid-response arrives as a bare OSError
                # rather than a URLError, and it is almost always transient.
                if attempt == 3:
                    raise SystemExit(f"could not reach github: {error}") from error
                time.sleep(2 ** attempt)
        raise SystemExit("github kept refusing the request; try again in a minute")

    def file(self, repo: str, path: str) -> str | None:
        payload = self.get(f"/repos/{repo}/contents/{path}")
        if not isinstance(payload, dict) or payload.get("encoding") != "base64":
            return None
        try:
            return base64.b64decode(payload["content"]).decode("utf-8", "replace")
        except (KeyError, ValueError):
            return None

    def search_issues(self, query: str, per_page: int = 10) -> dict:
        payload = self.get("/search/issues", q=query, per_page=str(per_page))
        return payload if isinstance(payload, dict) else {"total_count": 0, "items": []}


def token_from_gh_cli() -> str | None:
    if not shutil.which("gh"):
        return None
    try:
        done = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    token = done.stdout.strip()
    return token or None


def months_ago(months: int) -> str:
    today = dt.date.today()
    month = today.month - months
    year = today.year
    while month <= 0:
        month += 12
        year -= 1
    return dt.date(year, month, min(today.day, 28)).isoformat()


def find_policies(api: GitHub, repo: str) -> list[tuple[str, str, str, str]]:
    """(file, kind, meaning, the line it was found on)."""
    found: list[tuple[str, str, str, str]] = []
    for path in POLICY_FILES:
        text = api.file(repo, path)
        if not text:
            continue
        seen_in_file: set[str] = set()
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            for pattern, kind, meaning in POLICY_PATTERNS:
                if meaning in seen_in_file:
                    continue
                if re.search(pattern, stripped, re.IGNORECASE):
                    found.append((path, kind, meaning, stripped[:160]))
                    seen_in_file.add(meaning)
                    break
    return found


def merge_activity(api: GitHub, repo: str, months: int) -> dict:
    since = months_ago(months)
    exclusions = " ".join(f"-author:{bot}" for bot in BOT_AUTHORS)
    merged = api.search_issues(f"repo:{repo} is:pr is:merged merged:>{since} {exclusions}", 30)
    open_prs = api.search_issues(f"repo:{repo} is:pr is:open", 1)

    durations: list[float] = []
    for item in merged.get("items", [])[:30]:
        created, closed = item.get("created_at"), item.get("closed_at")
        if created and closed:
            opened_at = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
            merged_at = dt.datetime.fromisoformat(closed.replace("Z", "+00:00"))
            durations.append((merged_at - opened_at).total_seconds() / 86400)

    return {
        "merged": merged.get("total_count", 0),
        "open": open_prs.get("total_count", 0),
        "median_days": statistics.median(durations) if durations else None,
        "sampled": len(durations),
        "since": since,
    }


def report(repo: str, api: GitHub, months: int, duplicate_query: str | None) -> int:
    meta = api.get(f"/repos/{repo}")
    if not isinstance(meta, dict):
        print(f"{repo}: no such repository", file=sys.stderr)
        return 2

    print(f"# {meta['full_name']}")
    if meta.get("description"):
        print(f"  {meta['description']}")
    print(
        f"  {meta['stargazers_count']} stars, "
        f"last pushed {meta['pushed_at'][:10]}"
        + (", archived" if meta.get("archived") else "")
    )
    if meta.get("archived"):
        print("\n  Archived: it will not take a pull request.")
        return 1

    print("\n## Rules")
    policies = find_policies(api, repo)
    if not policies:
        print("  nothing found in " + ", ".join(POLICY_FILES[:4]) + ", ...")
    for path, kind, meaning, line in policies:
        print(f"  [{kind}] {path}: {meaning}")
        print(f"        {line}")

    print("\n## Pull requests")
    activity = merge_activity(api, repo, months)
    print(
        f"  {activity['merged']} merged since {activity['since']} "
        f"(bots excluded), {activity['open']} open now"
    )
    if activity["median_days"] is not None:
        print(
            f"  median {activity['median_days']:.1f} days from opening to merge, "
            f"over the {activity['sampled']} most recent"
        )
    if activity["merged"] == 0:
        print("  Nothing merged in the window: expect a long wait, or none.")
    elif activity["open"] > activity["merged"] * 5:
        print("  The backlog is much larger than the merge rate.")

    if duplicate_query:
        print(f"\n## Already reported?  ({duplicate_query})")
        for label, qualifier in (("issues", "is:issue"), ("pull requests", "is:pr")):
            result = api.search_issues(f"repo:{repo} {qualifier} {duplicate_query}", 8)
            total = result.get("total_count", 0)
            print(f"  {total} matching {label}")
            for item in result.get("items", []):
                state = item.get("state", "?")
                if item.get("pull_request", {}).get("merged_at"):
                    state = "merged"
                print(f"    #{item['number']} [{state}] {item['title'][:70]}")
        print("  Search both: the fix is often already in a pull request.")

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="contribcheck",
        description="Look a repository over before spending an evening on a pull request.",
        epilog='example: contribcheck cli/cli --dup "truncate emoji width"',
    )
    parser.add_argument("repos", nargs="+", metavar="OWNER/REPO")
    parser.add_argument(
        "--dup",
        metavar="TERMS",
        help="search the issues and the pull requests for these words",
    )
    parser.add_argument(
        "--months", type=int, default=6, help="window for the merge count, default 6"
    )
    parser.add_argument(
        "--token",
        help="GitHub token; otherwise GITHUB_TOKEN, otherwise `gh auth token`",
    )
    parser.add_argument("--version", action="version", version=f"contribcheck {__version__}")
    args = parser.parse_args(argv)

    api = GitHub(args.token or os.environ.get("GITHUB_TOKEN") or token_from_gh_cli())

    worst = 0
    for index, repo in enumerate(args.repos):
        if index:
            print()
        if "/" not in repo:
            print(f"{repo}: expected OWNER/REPO", file=sys.stderr)
            worst = max(worst, 2)
            continue
        worst = max(worst, report(repo, api, args.months, args.dup))
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
