---
applyTo: "src/**"
---

# Source Folder READMEs

Maintain a `README.md` in every folder under `src/`. The README documents that
folder's local slice of the system so an agent can understand its role without
reading every file. `.github/architecture/*.md` remain the source of truth for
global structure and package boundaries; folder READMEs describe only the
local slice and must not contradict or duplicate the architecture documents.

## Required Content

- Every folder under `src/` maintains a `README.md` that explains the purpose
  and boundaries of its subfolders and the architecture and design decisions
  of that slice of the system.
- Each folder README states the exact purpose of every code file at that
  folder level, with one entry per file using the file's current name.
- Explain what the folder contains, what it must not contain, and which
  neighbors it depends on or serves, matching the factual style of the
  architecture documents.

## Update Discipline

- Update the folder README in the same change whenever files or subfolders are
  added, removed, renamed, or repurposed.
- Keep statements factual and current. Do not describe planned behavior as
  implemented, and remove entries for files and subfolders that no longer
  exist.
- Update a stale README in the owning task rather than deferring the
  documentation to a later cleanup pass.

## Local Responsibility Contracts

- When adding or structurally changing a substantive module, maintain a concise
  module docstring (Python) or module documentation (Rust) stating its owned
  behavior/invariants, nearby responsibilities excluded, and required dependency
  capabilities. Describe the implementation truthfully; investigate existing
  responsibility mixtures rather than hiding them behind a narrow comment.
- Document meaningful interfaces from their consumer's need: behavioral
  promises, failure/ownership semantics, and authority excluded. Passive records,
  trivial helpers and re-export files do not need boilerplate contracts.
- Establish ownership after tracing current behavior and blast radius, before
  implementation, following the Implementation Phases in the organization
  instructions. Changing a comment does not justify unrelated responsibility.
- Keep global boundaries in architecture documents and the local file inventory
  in folder READMEs. Source contracts explain local ownership without duplicating
  those inventories. Presence checks cannot establish semantic compliance.
