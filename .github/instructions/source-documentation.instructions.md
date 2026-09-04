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
