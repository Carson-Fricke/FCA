# Global Knowledge Library

The Global Knowledge Library is the canonical, Git-versioned Markdown corpus
for trading, market structure, statistics, backtesting, data quality, risk,
execution, and FCA operational knowledge.  It is global: every workspace can
read it by default.  The canonical content is the Markdown itself — not a
database — and every page carries structured metadata in a pinned front-matter
contract so tools can parse, link, search, and cite it deterministically.

## Layout

- Pages are `*.md` files anywhere under this directory, recursively.  Each
  page is a normal Git-versioned Markdown file: front matter, then a free
  Markdown body.
- A `*.md` file that does not begin with a `---` front-matter delimiter is
  not a structured corpus page (this README, scratch notes) and is skipped
  by the corpus loader.  A file that begins front matter must satisfy the
  contract below exactly, or loading fails.
- `knowledge/index/` is derived, rebuildable search-index data.  It is
  non-canonical: it can be deleted and regenerated from the Markdown at any
  time and is never a second canonical copy.  Nothing canonical is stored
  there.

## Front-matter contract

Pages are `*.md` under the corpus root with YAML front matter delimited by
`---` lines, then Markdown body.  Fields:

- `id`: required stable kebab-case slug, unique across the corpus
- `title`: required non-empty string
- `version`: required positive int
- `created`: required ISO date (YYYY-MM-DD); `retrieved`: required ISO date
- `effective_start`, `effective_end`: optional ISO dates (end may be omitted
  for open-ended)
- `source`: required mapping {url: required http/https, publisher: optional
  str, content_hash: required 64-hex lowercase sha256}
- `tags`: required list of strings (may be empty)
- `links`: required list of strings (page ids or repository-relative corpus
  paths; may be empty)
- `relationships`: required list of mappings
  {type: "supersedes"|"contradicts"|"relates", target: page id} (may be empty)
- `trust`: required mapping
  {tier: "untrusted"|"curated"|"workspace_promoted"|"platform_policy",
  state: "draft"|"proposed"|"published"}

Body is free Markdown.

The strict parser and canonical renderer live in
`src/thesis/knowledge/metadata.py` (`parse_front_matter`,
`render_front_matter`).  Rendering is deterministic: the same metadata always
produces byte-identical front matter, and parsing a rendered page returns
semantically identical metadata.  Empty `tags`, `links`, and `relationships`
lists are written and rendered as `tags: []`; a bare `tags:` line with no
items is ill-formed.

## Typed relationships

A relationship is a directed, typed edge from this page to `target`, which is
a page id.  Semantics:

- `supersedes` — this page replaces the target page as the current statement
  of the topic.  The target remains in the corpus for history and citation of
  older states.  Inbound queries ("who supersedes this page?") are answered
  by `LinkGraph.superseded_by`.
- `contradicts` — this page asserts the opposite of the target page.  Both
  remain readable; resolving a contradiction is a curation act, not an
  automatic rewrite.  Inbound queries are answered by
  `LinkGraph.contradictions`.
- `relates` — a weak, non-directional association (same method, same market,
  shared caveat).  It carries no truth or precedence implication.

Declared `links` are plain hyperlinks between pages and may use either the
target page's id or its repository-relative corpus path
(e.g. `knowledge/markets/equity-premium.md`).  Dangling links or relationship
targets do not abort a load; they are reported by `CorpusSnapshot.validate()`
as structured problem lines (`dangling-link:`, `dangling-relationship:`), and
the strict `LinkGraph` constructor raises `LinkError` on them directly.

## Trust tiers and curation workflow

| Tier | Behavior |
| --- | --- |
| `platform_policy` | Signed platform policy. May instruct Thesis within its declared scope and has highest knowledge-tier precedence. |
| `workspace_promoted` | May guide methods or preferences in that workspace, but cannot override platform invariants or grant capabilities. |
| `curated` | Can support cited factual or methodological claims but is not an instruction source. |
| `untrusted` | Treated only as quoted external data and isolated from agent instructions. |

The `trust.state` field is the curation lifecycle: `draft`, `proposed`,
`published`.

Curation and publication are explicit Git operations.  Promoting a page's
trust tier or publishing it means editing its front matter `trust` mapping in
a reviewed, attributable commit — never an automatic side effect of fetching
or ingestion.  Fetched content enters the library as `untrusted` and stays
`untrusted` until a curator publishes it through such a commit; ingestion
proposals are always untrusted regardless of what their fetched front matter
declares.

Two structural trust rules are enforced by `CorpusSnapshot.validate()` as
`trust-mismatch:` problem lines:

- a `platform_policy` page must be `published`; and
- a `workspace_promoted` page cannot be `draft` (promotion is an explicit
  act, so a promoted page that is still a draft is a contradiction).

Workspace promotion changes interpretation inside that workspace only; it
never changes the global source Markdown or another workspace's trust.  The
first roadmap does not allow agent findings to publish directly into the
library.

## Rebuildable index

`knowledge/index/` holds derived lexical and semantic-search
representations generated from the canonical Markdown.  The index
implementation is replaceable, and the index is rebuildable data rather than
canonical content: it can be discarded and reconstructed from the Markdown
plus the corpus Git revision at any time.  Search results always retain the
Markdown path, Git revision, page version, and source provenance so a
workflow can cite the exact corpus state it used.
