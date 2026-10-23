# SDK identity and complete delivery verification — 2026-10-23

## Diagnosis and result

The running harness served SDK identity
`111d6ad7108c020e12325a82261eff0a39e117ad802e8aab2f904e73d005f457`,
which matched the committed SDK. Local files differed for two independent
reasons: Windows CRLF checkout bytes and an uncommitted substantive change to
`strategy-sdk/src/building_blocks.rs`. Normalizing newlines alone cannot publish
that change. The complete pending repository delivery includes it and its tests.

The expected current SDK identity after LF normalization is
`84858ae349b9047a5f83cebbf81e32f1d8c088c92b3ec3ea2b59f418ef582aa6`
across 35 source/manifest entries. Deployment confirmation is a separate live
check after this commit is pushed; this pre-delivery record does not assert that
deployment has already occurred.

## Implementation and limits

- Git attributes, editor settings, physical normalization, repository hooks and
  CI enforce LF text. Binary assets and ignored local data are excluded.
  The normalizer converted 1,190 working files without changing other bytes.
- Python and Rust SDK source inventories canonicalize CRLF to LF before hashing.
  Content changes, lone CR, whitespace and final-newline differences remain
  significant. Program digests and compiler provenance retain their contracts.
- SDK identity/version/ABI matching remains mandatory. Real source drift still
  prevents authoring/validation/execution, with differing source paths reported.
  The remote harness has not become the sole source of SDK authority.
- Delivery instructions now include every pending non-ignored change, including
  pre-existing edits and SDK producers/consumers. The pre-push hook rejects
  uncommitted staged, unstaged and untracked files. An exclusion requires an
  explicit human request and a reason scoped to that push.
- Hooks must be installed in each checkout. CI independently checks LF, while
  local delivery guards and instructions prevent accidental omissions. Pushing
  is not deployment; serving identity must be checked after rollout.

## Measured behavior

The HTTP serving composition test uses a fixed LF serving SDK and local LF,
CRLF and mixed source copies across all four SDK crates. Before the hashing
change, LF passed and both newline variants failed. Afterward, all three author,
validate and execute successfully. A genuine SDK source edit is still rejected
and names the differing file. Python and Rust share a UTF-8 digest golden vector;
negative cases preserve content sensitivity. The delivery CLI rejected the real
dirty checkout before staging/commit.

## Frozen integration verification

Production sources and tests were frozen before verification. A digest comparison
afterward found only the intentional pre-push hook adjustment to check both index
and physical working files; this record was added after verification.

| Verification | Result |
| --- | --- |
| `uv lock --check` | Passed |
| `uv run --extra dev ruff check .` | Passed |
| `uv run --extra dev ruff format --check .` | Passed |
| `uv run --extra dev ty check` | Passed |
| `uv run --extra dev pytest -p no:cacheprovider --durations=25` | 1,706 passed, 1 opt-in live Cosmos test skipped; 153.80 s |
| Focused SDK/serving/workspace/delivery guard tests | 201 passed |
| Node release tooling tests | 14 passed |
| Harness and lake workspace Rust formatting | Passed, Rust 1.98.1 |
| Harness and lake strict workspace Clippy, all targets, locked | Passed |
| Harness Rust coverage | 354 passed across full run progress plus remaining targets; see timeout detail below |
| Lake Rust workspace tests, locked, no-fail-fast | 133 passed; 106.41 s including compilation |
| UI type/component checks | Zero errors; two existing CSS warnings |
| UI tests | 288 passed, 1 existing skipped |
| UI production build | Passed |
| Bicep build and lint | Passed with existing warnings |
| Wheel build and isolated installed SDK graph/reference/template smoke | Passed |
| Physical text LF and staged whitespace checks | Passed |

The full Windows harness command reached its 360 s hard deadline after 157 s
of compilation and 325 passing cases, with no failed assertions. Following the
repository timeout policy, it was not rerun unchanged. The next binary,
`mcp/remote_certainty_sdk`, was isolated with a 60 s deadline: all 3 tests passed.
The remaining MCP targets (4), num targets (21), and workspace doctest (1) passed.
This completes target coverage but is not a claim that the original full Windows
command exited successfully. Linux CI provides an independent full-command run.

## Combined delivery scope

The human explicitly requested all pending changes be pushed. This delivery also
includes the already-pending PostgreSQL persistence, research/backtest catalog,
partial-fill accounting, local stack and UI/history changes. Their existing
verification records are retained unchanged. The integration results above cover
the combined checkout, including the mandatory delivery architecture guard and
PostgreSQL behavior tests.
