# Compare saved contribution reports

Save JSON reports at two times using the same author and options, then compare
them locally:

```sh
python3 contribution_report.py Doribelove --format json --output before.json
# Run the next export when you want to check for changes.
python3 contribution_report.py Doribelove --format json --output after.json
python3 compare_reports.py before.json after.json
python3 compare_reports.py before.json after.json --format json --output changes.json
```

The comparison uses only the two UTF-8 JSON files. It needs Python 3.10 or later
and this repository's scripts; it does not call GitHub or require an installed
or authenticated `gh`. UTF-8 files with a byte-order mark are also accepted.
Pass files in the desired before/after order; their generation times appear in
the result. The comparison preserves this order rather than sorting by date.

`--output PATH` saves the completed comparison as UTF-8 and leaves stdout empty.
An invalid snapshot or failed write keeps an existing output file intact. The
output must be distinct from both input snapshots and cannot be a symbolic link;
its parent directory must already exist. See [file export behavior](../README.md#save-reports-safely).

The result separates:

- **Added**: PRs present only in the after snapshot, with their observed metadata.
- **Removed**: PRs present only in the before snapshot, with their last observed
  metadata. This means absent from the later report, not deleted from GitHub.
- **Changed**: PRs in both snapshots with individual before/after values for
  `state`, `reviewDecision`, `isDraft`, `updatedAt`, `mergedAt`, `title`, or
  `repository.isFork`. A change to `updatedAt` alone is included.
- **Unchanged**: the count of shared PRs whose compared fields stayed the same.

For `updatedAt` and `mergedAt`, the comparison uses the instant in time.
For example, `08:00:00+08:00` and `00:00:00.000Z` on the same date match.
Differences smaller than a microsecond are preserved. When a timestamp changes,
the result shows the original text from each snapshot.

Added does not necessarily mean newly created. Visibility changes, repository
renames or transfers, and GitHub search indexing can affect membership. In an
open-only report, a PR that becomes merged or closed may appear under removed;
the saved snapshots cannot establish which event caused its absence. Use reports
without a state filter when you want to observe state transitions directly.
Snapshots show the endpoints, not every event that occurred between them.

## Matching author and filters

Both files must use the same author, scope and search filters. This includes
`--include-own`, repository, state, and any date filters. Comparing a full-history
report with a recent-activity report, or moving a date cutoff between exports,
returns an error. Author and repository name capitalization is ignored, including
in PR identity, scope and search qualifiers. Search whitespace is normalized;
other search terms and scope text must match.

The tool checks required report fields, PR URLs, timestamps, relationships,
duplicate PRs and row counts before generating output. A canonical PR link must
point to `https://github.com/OWNER/REPO/pull/NUMBER` and agree with its row.
Markdown escapes titles and displays each changed field with its old and new
value. JSON retains the original titles and includes complete before/after PR
records for changed entries, plus `added`, `removed`, `changed`, `counts`, the
two generation times, and a membership explanation.

Invalid files or incompatible snapshots produce a concise error on stderr and
exit status 1 with empty stdout. Invalid CLI arguments use exit status 2.
Successful comparisons, including empty or identical snapshots, return 0.

Run the offline tests with:

```sh
python3 -m unittest -v test_compare_reports
```
