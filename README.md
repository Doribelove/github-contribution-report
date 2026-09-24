# GitHub Contribution Report

A small command-line GitHub API integration that reports a developer's public
authored pull requests. It answers a recurring contribution-tracking question:
which changes were merged, which remain open, and which were closed without merge?
Reports are printed as Markdown or JSON for use in local records or a manually
maintained portfolio.

See a [real public contribution report](examples/Doribelove-2026-09-23.md)
generated on September 23, 2026. This is a dated snapshot; run the command
above to check current pull request states.

## Requirements and setup

- Python 3.10 or later; no Python packages to install.
- [GitHub CLI](https://cli.github.com/) installed and authenticated to GitHub.com
  using `gh auth login --hostname github.com`.

Clone this repository, then run the script from its directory:

```sh
python3 contribution_report.py Doribelove
python3 contribution_report.py Doribelove --format json > contributions.json
python3 contribution_report.py Doribelove --include-own > contributions.md
python3 contribution_report.py Doribelove --state open
python3 contribution_report.py Doribelove --state merged
python3 contribution_report.py Doribelove --repo pydata/sparse
python3 contribution_report.py Doribelove --repo Doribelove/github-contribution-report --include-own
python3 contribution_report.py Doribelove --gh /path/to/gh
```

The default report includes public PRs to repositories owned by other accounts
or organizations. `--include-own` also includes the author's personal repositories
and labels them `own`. Ownership comparison is case-insensitive. Organization
membership and project independence are not inferred: a repository owned by an
organization is categorized as `external`, even if the author belongs to it.
Destination repositories that are forks retain an explicit `isFork` field.

## What the report means

- `MERGED` counts as merged; `CLOSED` counts as closed without merge.
- Draft PRs are counted within open PRs and also summarized separately.
- `--state open`, `--state merged`, or `--state closed` filters the displayed
  rows and `counts` after fetching all public authored PRs. `closed` means
  closed without merge. The `public_authored_total` and `own_repository_total`
  fields still describe all fetched PRs, before either state or ownership
  filtering. A filter with no matches produces an empty report.
- `--repo OWNER/NAME` limits the API search to one destination repository.
  `public_authored_total` and `own_repository_total` then describe that
  repository's search results, before state or ownership filtering. The normal
  external-only default still applies, so use `--include-own` for a repository
  owned by the author.
- Each PR includes its URL, title, number, state, draft status, merge/update times,
  review decision, repository ownership, and fork status.
- Public authored PR totals and own-repository totals describe all fetched PRs;
  the `counts` and `pull_requests` fields use the selected report scope.
- An empty search produces an empty report. It does not verify that the username
  exists. GitHub search indexing may lag behind a recent change.

The tool uses the [GitHub GraphQL search API](https://docs.github.com/en/graphql/reference)
through [`gh api graphql`](https://cli.github.com/manual/gh_api), requesting up to
100 PRs per page. It follows cursors and checks that all reported search results
were fetched. GitHub search exposes at most 1,000 results for the selected
query: larger result sets cause an explicit error, not a misleading partial
report. `--repo` can narrow that search. Changed counts, duplicate results, or
stalled pagination also fail with a retry message.
Pagination is not a transactional snapshot; PR metadata can change during a run.

## Data handling

Only public PR metadata is requested, always from `github.com`, including when
`GH_HOST` is set to a different host. GitHub CLI manages authentication; this
script never reads or stores credentials. No shell commands are constructed from
user input. No report is uploaded or published, and no scheduler is installed.
Output goes to stdout only after all pages succeed. Errors go to stderr with
exit status 1; invalid arguments use argparse's exit status 2. Raw CLI output is
not echoed on API failures, so use `gh auth status` separately for diagnostics.

## Development

Run the standard-library test suite without network access:

```sh
python3 -m unittest -v
```

Tests mock GitHub CLI transport and cover pagination, the search cap, empty
results, incomplete results, repository and ownership filtering, state counts,
API failures, input validation, and Markdown escaping.

Support: [doribelove@gmail.com](mailto:doribelove@gmail.com). Bug reports with a
minimal reproduction are welcome; do not include credentials or private data.

AI assistance: the initial implementation, tests, and documentation were prepared
with Codex. Test results should be checked before relying on a report.

Licensed under the [MIT License](LICENSE). This is an independent integration and
does not claim affiliation with GitHub or award GitHub profile achievements.
