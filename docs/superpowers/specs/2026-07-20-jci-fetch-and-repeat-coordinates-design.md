# JCI Fetch Fallback And Repeated-Segment Coordinates Design

## Goal

Make the DOI-first workflow retrieve public JCI supporting-data tables when
Europe PMC archive endpoints fail, and make `within_row_repeated_segment`
findings identify the exact spreadsheet row and repeated column ranges.

## Scope

This change has two production behaviors:

1. A JCI-specific public attachment fallback for DOI candidates matching
   `10.1172/JCI<number>`.
2. Exact row and Excel column-range coordinates for
   `within_row_repeated_segment` findings and their HTML rendering.

The detector thresholds, candidate ranking, profile behavior, and existing
archive extraction rules remain unchanged.

Release work publishes the already-versioned `0.8.3` package after code review
and verification. No paper source data, DOI-specific fixture, judgment, or
credential is committed.

## JCI Attachment Fallback

### Trigger

The fallback runs only when all of these conditions hold:

- the selected candidate DOI matches `10.1172/JCI<number>`, case-insensitively;
- normal direct tabular downloads, PMC OA-package extraction, and Europe PMC
  supplementary-archive extraction have produced no tabular file;
- the candidate is otherwise a confident match selected by the existing fetch
  workflow.

This keeps repository candidates and healthy Europe PMC downloads on their
current path.

### Resolution

The resolver derives the numeric article identifier from the DOI and requests:

```text
https://www.jci.org/articles/view/<number>/sd/3
```

It parses public attachment links from that page and admits only HTTP(S) links
whose filename has a supported tabular extension. Relative and scheme-relative
links are resolved against the JCI page URL.

The resolver does not guess CloudFront filenames and does not bypass access
controls. Resolution failure is returned as a normal skipped-download reason;
it must not erase the original Europe PMC failure context.

### Download And Provenance

Resolved attachments pass through the existing `download_file` and secure
publication pipeline, including:

- HTTPS URL validation and redirect checks;
- HTML-response rejection and content sniffing;
- per-file and per-paper size caps;
- no-follow staging and collision-safe publication;
- provenance sidecar generation.

The provenance candidate remains the matched Europe PMC paper candidate. The
download entry records the final official attachment source URL.

## Repeated-Segment Coordinates

`within_row_repeated_segment` retains its current detection and deduplication
logic. Each emitted finding adds:

```json
{
  "row": 19,
  "row_idx": 18,
  "occurrences": [
    {"row": 19, "col_start": 2, "col_end": 5, "range": "B:E"},
    {"row": 19, "col_start": 8, "col_end": 11, "range": "H:K"}
  ]
}
```

Conventions:

- `row` is the 1-based Excel row shown to users.
- `row_idx` is the existing 0-based internal row index.
- `col_start` and `col_end` are 1-based inclusive Excel column numbers.
- `range` is the human-readable Excel column range.
- Occurrences are emitted in ascending column order.

The rule text includes the row and ranges:

```text
the 4-value segment [...] repeats at 2 non-overlapping positions within row 19
of F2 (B:E ↔ H:K)
```

The raw HTML report renders the coordinates in the finding summary/evidence so
a reviewer can open the source table directly. Existing consumers that ignore
the new fields remain compatible.

## Tests

All production changes follow red-green TDD.

### Fetch tests

- A JCI DOI with failed Europe PMC/OA archive paths resolves `/sd/3`, downloads
  the public table, and records provenance.
- A non-JCI DOI never invokes the JCI resolver.
- A JCI page with no supported table returns a bounded skipped reason.
- Unsafe, malformed, non-tabular, and HTML attachment responses remain rejected
  by existing download policy.
- CLI `fetch --auto` reports one downloaded file for the synthetic JCI case.

### Coordinate tests

- A synthetic repeated row emits the expected 1-based row, zero-based row index,
  ordered occurrences, and Excel ranges.
- The HTML report contains the exact `row N` and `B:E ↔ H:K`-style coordinates.
- Existing repeated-segment false-positive gates remain green.

### Verification

Run focused fetch and detector/report tests first, then the complete test suite.
Build both wheel and sdist, inspect their metadata and contents, install the
wheel in a clean temporary environment, and run CLI smoke checks before release.

## Review And Release

After implementation:

1. Dispatch an independent code-review subagent against the implementation
   diff and this specification.
2. Resolve every Critical or Important finding and rerun verification.
3. Commit only files belonging to this change and push `main`.
4. Confirm PyPI does not already contain `0.8.3`.
5. Build and verify distributions from the pushed commit.
6. Publish `paperconan==0.8.3` using the repository's available PyPI
   credentials or trusted-publishing path.
7. Verify the PyPI JSON API and a clean installation report version `0.8.3`.
8. Tag the exact published commit as `v0.8.3` and push the tag.

If publishing credentials are unavailable, stop after the verified push and
report the exact external action required; do not claim that the release was
published.
