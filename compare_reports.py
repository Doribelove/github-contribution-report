#!/usr/bin/env python3
"""Compare two saved contribution-report JSON snapshots without network access."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import sys

import contribution_report as report


MEMBERSHIP_NOTE = (
    "Added and removed describe membership in these snapshots. Removed means absent "
    "from the after snapshot, not deleted from GitHub; filters, visibility or search "
    "indexing can affect membership."
)
CHANGE_FIELDS = (
    "state", "reviewDecision", "isDraft", "updatedAt", "mergedAt", "title",
    "repository.isFork",
)
TIMESTAMP_PATTERN = re.compile(
    r"(?P<second>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})"
    r"(?:\.(?P<fraction>[0-9]+))?(?P<offset>Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)


class ComparisonError(Exception):
    """The supplied files cannot form a reliable snapshot comparison."""


def timestamp_key(value):
    """Compare whole seconds with their offset and retain exact fractional precision."""
    match = TIMESTAMP_PATTERN.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("expected an ISO timestamp with a time zone")
    second = datetime.fromisoformat(match["second"] + match["offset"].replace("Z", "+00:00"))
    return second, (match["fraction"] or "").rstrip("0")


def normalized_scope(value):
    """Repository names are case-insensitive; all other scope text is retained."""
    return "; ".join(
        part.casefold() if part.startswith("repository: ") else part
        for part in value.split("; ")
    )


def normalized_search(value, username):
    words = value.split()
    authors = [word[7:] for word in words if word.startswith("author:")]
    if len(authors) != 1 or authors[0].casefold() != username.casefold():
        raise ValueError("search_query must name the snapshot author exactly once")
    if "is:pr" not in words or "is:public" not in words:
        raise ValueError("search_query must select public pull requests")
    for word in words:
        if word.startswith("repo:"):
            report.valid_repository(word[5:])
    return " ".join(
        word.casefold() if word.startswith(("author:", "repo:")) else word
        for word in words
    )


def pr_key(pr):
    return pr["repository"]["nameWithOwner"].casefold(), pr["number"]


def validate_snapshot(snapshot, label):
    """Check saved report structure before using any rows or rendering links."""
    try:
        if not isinstance(snapshot, dict):
            raise ValueError("expected a JSON object")
        if not isinstance(snapshot["username"], str):
            raise ValueError("username must be a string")
        username = report.valid_username(snapshot["username"])
        timestamp_key(snapshot["generated_at"])
        for field in ("scope", "search_query"):
            if not isinstance(snapshot[field], str) or not snapshot[field].strip():
                raise ValueError(f"{field} must be nonempty text")
            snapshot[field].encode("utf-8")
        normalized_search(snapshot["search_query"], username)
        if not isinstance(snapshot["pull_requests"], list):
            raise ValueError("pull_requests must be a list")
        for field in ("public_authored_total", "own_repository_total"):
            if type(snapshot[field]) is not int or snapshot[field] < 0:
                raise ValueError(f"{field} must be a nonnegative integer")
        if snapshot["own_repository_total"] > snapshot["public_authored_total"]:
            raise ValueError("own_repository_total exceeds public_authored_total")
        counts = {"total": len(snapshot["pull_requests"]), "merged": 0,
                  "open": 0, "closed_unmerged": 0, "draft": 0}
        seen, own_count = set(), 0
        for pr in snapshot["pull_requests"]:
            report.validate_pr(pr)
            pr["title"].encode("utf-8")
            report.valid_repository(pr["repository"]["nameWithOwner"])
            # urlsplit normalizes some control characters, so also require the raw
            # canonical URL before placing it in a Markdown link.
            expected_url = (f"https://github.com/{pr['repository']['nameWithOwner']}"
                            f"/pull/{pr['number']}")
            if pr["url"] != expected_url:
                raise ValueError("PR URL must be canonical")
            timestamp_key(pr["updatedAt"])
            if pr["mergedAt"] is not None:
                timestamp_key(pr["mergedAt"])
            own = pr["repository"]["owner"]["login"].casefold() == username.casefold()
            if pr["relationship"] != ("own" if own else "external"):
                raise ValueError("PR relationship disagrees with its owner")
            own_count += own
            key = pr_key(pr)
            if key in seen:
                raise ValueError("duplicate pull request")
            seen.add(key)
            counts[{"OPEN": "open", "MERGED": "merged", "CLOSED": "closed_unmerged"}[pr["state"]]] += 1
            counts["draft"] += pr["state"] == "OPEN" and pr["isDraft"]
        if not isinstance(snapshot["counts"], dict):
            raise ValueError("counts must be an object")
        for field, expected in counts.items():
            if type(snapshot["counts"][field]) is not int or snapshot["counts"][field] != expected:
                raise ValueError(f"counts.{field} does not match pull_requests")
        if (counts["total"] > snapshot["public_authored_total"]
                or own_count > snapshot["own_repository_total"]
                or counts["total"] - own_count > (
                    snapshot["public_authored_total"] - snapshot["own_repository_total"])):
            raise ValueError("selected PRs exceed the reported search totals")
    except (report.ReportError, argparse.ArgumentTypeError, AttributeError,
            KeyError, TypeError, ValueError) as exc:
        reason = str(exc) if not isinstance(exc, (KeyError, TypeError, AttributeError)) else "missing or invalid fields"
        raise ComparisonError(f"{label} snapshot: {reason}") from exc


def field_value(pr, field):
    if field == "repository.isFork":
        return pr["repository"]["isFork"]
    return pr[field]


def compare_snapshots(before, after):
    validate_snapshot(before, "before")
    validate_snapshot(after, "after")
    if before["username"].casefold() != after["username"].casefold():
        raise ComparisonError("snapshots have different authors")
    if (normalized_scope(before["scope"]) != normalized_scope(after["scope"])
            or normalized_search(before["search_query"], before["username"])
            != normalized_search(after["search_query"], after["username"])):
        raise ComparisonError("snapshots have different scopes or search filters; export both with the same options")

    old = {pr_key(pr): pr for pr in before["pull_requests"]}
    new = {pr_key(pr): pr for pr in after["pull_requests"]}
    added = [new[key] for key in sorted(new.keys() - old.keys())]
    removed = [old[key] for key in sorted(old.keys() - new.keys())]
    changed = []
    for key in sorted(old.keys() & new.keys()):
        changes = {}
        for field in CHANGE_FIELDS:
            previous = field_value(old[key], field)
            current = field_value(new[key], field)
            if previous == current:
                continue
            if (field in ("updatedAt", "mergedAt") and previous is not None and current is not None
                    and timestamp_key(previous) == timestamp_key(current)):
                continue
            changes[field] = {"before": previous, "after": current}
        if changes:
            changed.append({"before": old[key], "after": new[key], "changes": changes})
    return {
        "username": after["username"], "scope": after["scope"],
        "search_query": after["search_query"],
        "before_generated_at": before["generated_at"],
        "after_generated_at": after["generated_at"],
        "membership_note": MEMBERSHIP_NOTE,
        "counts": {"before": len(old), "after": len(new), "added": len(added),
                   "removed": len(removed), "changed": len(changed),
                   "unchanged": len(old.keys() & new.keys()) - len(changed)},
        "added": added, "removed": removed, "changed": changed,
    }


def pr_link(pr):
    label = report.markdown_text(f"{pr['repository']['nameWithOwner']}#{pr['number']}")
    return f"[{label}](<{pr['url']}>)"


def display_value(value):
    if value is None:
        return "—"
    if type(value) is bool:
        return str(value).lower()
    return report.markdown_text(value)


def to_markdown(comparison):
    counts = comparison["counts"]
    lines = [
        f"# GitHub contribution changes: {comparison['username']}", "",
        f"Before: {comparison['before_generated_at']}",
        f"After: {comparison['after_generated_at']}", "",
        f"Scope: {report.markdown_text(comparison['scope'])}", "",
        f"Added: **{counts['added']}** · Removed: **{counts['removed']}** · "
        f"Changed: **{counts['changed']}** · Unchanged: **{counts['unchanged']}**.", "",
        MEMBERSHIP_NOTE, "",
    ]
    for name, heading in (("added", "Added to snapshot"),
                          ("removed", "Removed from snapshot (last observed values)")):
        if not comparison[name]:
            continue
        lines.extend([f"## {heading}", "",
                      "| Pull request | Title | State | Review | Draft | Updated |",
                      "| --- | --- | --- | --- | --- | --- |"])
        for pr in comparison[name]:
            values = [pr[field] for field in ("title", "state", "reviewDecision", "isDraft", "updatedAt")]
            lines.append("| " + " | ".join([pr_link(pr)] + [display_value(value) for value in values]) + " |")
        lines.append("")
    if comparison["changed"]:
        lines.extend(["## Changed pull requests", "",
                      "| Pull request | Current title | Field | Before | After |",
                      "| --- | --- | --- | --- | --- |"])
        for item in comparison["changed"]:
            for field, change in item["changes"].items():
                values = [item["after"]["title"], field, change["before"], change["after"]]
                lines.append("| " + " | ".join([pr_link(item["after"])] +
                                              [display_value(value) for value in values]) + " |")
        lines.append("")
    if not any(counts[field] for field in ("added", "removed", "changed")):
        lines.append("No pull request changes between these snapshots.")
    return "\n".join(lines).rstrip() + "\n"


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ComparisonError("duplicate JSON field")
        result[key] = value
    return result


def reject_constant(value):
    raise ComparisonError("JSON must not contain non-finite numbers")


def load_snapshot(path, label):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"),
                          object_pairs_hook=unique_object, parse_constant=reject_constant)
    except json.JSONDecodeError as exc:
        raise ComparisonError(f"{label} snapshot: invalid JSON at line {exc.lineno}, column {exc.colno}") from exc
    except (OSError, UnicodeError) as exc:
        raise ComparisonError(f"{label} snapshot: could not read UTF-8 JSON; check the file path and encoding") from exc
    except (ComparisonError, RecursionError, ValueError) as exc:
        raise ComparisonError(f"{label} snapshot: invalid JSON structure") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", help="earlier JSON snapshot")
    parser.add_argument("after", help="later JSON snapshot with the same author and filters")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--output", metavar="PATH", help="save UTF-8 output to a file distinct from both snapshots")
    args = parser.parse_args(argv)
    try:
        if args.output is not None:
            destination = Path(args.output)
            for source in (Path(args.before), Path(args.after)):
                if (destination.resolve() == source.resolve()
                        or (destination.exists() and source.exists() and destination.samefile(source))):
                    raise ComparisonError("output path must be different from both input snapshots")
        comparison = compare_snapshots(load_snapshot(args.before, "before"),
                                       load_snapshot(args.after, "after"))
        output = (json.dumps(comparison, ensure_ascii=False, indent=2) + "\n"
                  if args.format == "json" else to_markdown(comparison))
        report.write_output(output, args.output)
    except (ComparisonError, report.ReportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (OSError, RuntimeError):
        print("error: could not inspect output path; check its directory and permissions", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
