#!/usr/bin/env python3
"""Report public authored GitHub pull requests using authenticated GitHub CLI."""

import argparse
from datetime import datetime, timezone
import html
import json
import re
import subprocess
import sys
from urllib.parse import urlsplit


QUERY = """
query ContributionReport($search: String!, $after: String) {
  search(query: $search, type: ISSUE, first: 100, after: $after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        url title number state isDraft mergedAt updatedAt reviewDecision
        repository { nameWithOwner isFork owner { login } }
      }
    }
  }
}
"""
SEARCH_LIMIT = 1000
STATE_FILTERS = {"open": "OPEN", "merged": "MERGED", "closed": "CLOSED"}


class ReportError(Exception):
    """The API could not supply a complete report."""


def valid_username(value):
    """Reject search qualifiers and other input that is not a GitHub login."""
    if not 1 <= len(value) <= 39 or not re.fullmatch(
        r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", value
    ):
        raise argparse.ArgumentTypeError("expected a GitHub username, not a URL or query")
    return value


def fetch_page(gh, search, cursor):
    request = {"query": QUERY, "variables": {"search": search, "after": cursor}}
    try:
        result = subprocess.run(
            [gh, "api", "graphql", "--hostname", "github.com", "--input", "-"],
            input=json.dumps(request), text=True, capture_output=True,
            timeout=60, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ReportError("GitHub API request timed out after 60 seconds") from exc
    except OSError as exc:
        raise ReportError("could not execute GitHub CLI; check --gh and installation") from exc
    if result.returncode:
        # Do not echo subprocess output: gh handles credentials and may have debug logging.
        raise ReportError(
            f"gh api failed (exit {result.returncode}); check gh auth status "
            "and GitHub API availability/rate limits"
        )
    try:
        payload = json.loads(result.stdout)
        if payload.get("errors"):
            raise ReportError("GitHub returned GraphQL errors; no partial report was emitted")
        page = payload["data"]["search"]
        count = page["issueCount"]
        info = page["pageInfo"]
        if type(count) is not int or count < 0 or not isinstance(page["nodes"], list):
            raise ValueError("invalid count or nodes")
        if type(info["hasNextPage"]) is not bool:
            raise ValueError("invalid page info")
        if info["endCursor"] is not None and not isinstance(info["endCursor"], str):
            raise ValueError("invalid cursor")
        return page
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ReportError("GitHub returned an invalid API response") from exc


def validate_pr(pr):
    """Fail closed if a page contains inaccessible or unexpected search nodes."""
    try:
        repo = pr["repository"]
        for value in (pr["url"], pr["title"], pr["updatedAt"],
                      repo["nameWithOwner"], repo["owner"]["login"]):
            if not isinstance(value, str):
                raise ValueError("missing string")
        if pr["state"] not in {"OPEN", "CLOSED", "MERGED"}:
            raise ValueError("unknown PR state")
        if type(pr["number"]) is not int or pr["number"] < 1:
            raise ValueError("invalid PR number")
        if type(pr["isDraft"]) is not bool or type(repo["isFork"]) is not bool:
            raise ValueError("invalid boolean")
        if pr["reviewDecision"] not in {None, "APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED"}:
            raise ValueError("invalid review decision")
        if pr["mergedAt"] is not None and not isinstance(pr["mergedAt"], str):
            raise ValueError("invalid merged date")
        if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repo["nameWithOwner"]):
            raise ValueError("invalid repository name")
        if repo["nameWithOwner"].split("/")[0].lower() != repo["owner"]["login"].lower():
            raise ValueError("inconsistent repository owner")
        url = urlsplit(pr["url"])
        expected_path = f"/{repo['nameWithOwner']}/pull/{pr['number']}"
        if (url.scheme, url.netloc, url.path, url.query, url.fragment) != (
            "https", "github.com", expected_path, "", ""
        ):
            raise ValueError("unexpected PR URL")
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ReportError("GitHub returned an incomplete or unexpected pull request") from exc


def collect_report(username, gh="gh", include_own=False, state_filter=None):
    username = valid_username(username)
    if state_filter is not None and state_filter not in STATE_FILTERS:
        raise ValueError("state_filter must be open, merged, or closed")
    search = f"is:pr is:public author:{username} sort:updated-desc"
    prs, seen_urls, seen_cursors = [], set(), set()
    cursor, expected_count = None, None
    while True:
        page = fetch_page(gh, search, cursor)
        count = page["issueCount"]
        if count > SEARCH_LIMIT:
            raise ReportError(
                f"search matches {count} PRs, exceeding GitHub's 1000-result search cap; "
                "this tool cannot provide a complete report"
            )
        if expected_count is not None and count != expected_count:
            raise ReportError("search results changed during pagination; retry for a complete report")
        expected_count = count
        for pr in page["nodes"]:
            validate_pr(pr)
            if pr["url"] in seen_urls:
                raise ReportError("duplicate PR across search pages; retry for a complete report")
            seen_urls.add(pr["url"])
            prs.append(pr)
        info = page["pageInfo"]
        if not info["hasNextPage"]:
            break
        cursor = info["endCursor"]
        if not page["nodes"] or not cursor or cursor in seen_cursors or len(prs) >= SEARCH_LIMIT:
            raise ReportError("pagination stopped progressing or reached the 1000-result search cap")
        seen_cursors.add(cursor)
    if len(prs) != expected_count:
        raise ReportError("search returned an incomplete result set; retry for a complete report")

    own_count = sum(pr["repository"]["owner"]["login"].lower() == username.lower() for pr in prs)
    selected = []
    for pr in prs:
        own = pr["repository"]["owner"]["login"].lower() == username.lower()
        if (own and not include_own) or (state_filter and pr["state"] != STATE_FILTERS[state_filter]):
            continue
        selected.append(dict(pr, relationship="own" if own else "external"))
    counts = {"total": len(selected), "merged": 0, "open": 0, "closed_unmerged": 0, "draft": 0}
    for pr in selected:
        counts[{"MERGED": "merged", "OPEN": "open", "CLOSED": "closed_unmerged"}[pr["state"]]] += 1
        counts["draft"] += pr["state"] == "OPEN" and pr["isDraft"]
    return {
        "username": username,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": ("public, including own repositories" if include_own else "public, external repositories")
        + (f"; state: {state_filter}" if state_filter else ""),
        "search_query": search,
        "public_authored_total": len(prs),
        "own_repository_total": own_count,
        "counts": counts,
        "pull_requests": selected,
    }


def markdown_text(value):
    text = html.escape(str(value), quote=False)
    text = " ".join(text.split())
    return re.sub(r"([\\`*{}_\[\]()|])", r"\\\1", text)


def to_markdown(report):
    counts = report["counts"]
    lines = [
        f"# GitHub contribution report: {report['username']}", "",
        f"Generated: {report['generated_at']}", "",
        f"Scope: {report['scope']}.", "",
        f"Merged: **{counts['merged']}** · Open: **{counts['open']}** "
        f"(draft: {counts['draft']}) · Closed without merge: **{counts['closed_unmerged']}**.", "",
        "External means the destination repository is owned by another account or organization; "
        "it does not imply project independence or maintainer endorsement.", "",
        "| Pull request | Title | State | Review | Relationship | Destination is fork | Updated | Merged |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for pr in report["pull_requests"]:
        label = markdown_text(f"{pr['repository']['nameWithOwner']}#{pr['number']}")
        # Canonical GitHub URLs were checked before rendering. Angle brackets protect spaces/parentheses.
        link = f"[{label}](<{pr['url']}>)"
        values = [pr["title"], "DRAFT" if pr["isDraft"] and pr["state"] == "OPEN" else pr["state"],
                  pr["reviewDecision"] or "—", pr["relationship"], str(pr["repository"]["isFork"]).lower(),
                  pr["updatedAt"], pr["mergedAt"] or "—"]
        lines.append("| " + " | ".join([link] + [markdown_text(value) for value in values]) + " |")
    if not report["pull_requests"]:
        lines.extend(["", "No matching public pull requests."])
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username", type=valid_username)
    parser.add_argument("--gh", default="gh", help="GitHub CLI executable (default: gh)")
    parser.add_argument("--include-own", action="store_true", help="include PRs to repositories owned by the author")
    parser.add_argument("--state", choices=STATE_FILTERS, help="show only open, merged, or closed-unmerged PRs")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    args = parser.parse_args(argv)
    try:
        report = collect_report(args.username, args.gh, args.include_own, args.state)
    except ReportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    output = json.dumps(report, ensure_ascii=False, indent=2) + "\n" if args.format == "json" else to_markdown(report)
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
