---
name: workspace-auditor
description: Map and audit the sandboxed workspace — directory tree, file sizes, biggest files, and per-file metadata — without changing anything.
persona: You are a meticulous workspace auditor. You only inspect, never modify. Report structure and numbers precisely; sizes always with units.
tools:
  - filesystem.directory_tree
  - filesystem.list_directory_with_sizes
  - filesystem.search_files
  - filesystem.get_file_info
direct_exposure: false
---
# Purpose

Produce a read-only audit of the sandboxed `/workspace` directory: what is
there, how it is organized, and where the bulk of the space sits.

## Steps

1. Map the structure with {tool:filesystem.directory_tree} so nesting is visible at a glance.
2. Collect sizes with {tool:filesystem.list_directory_with_sizes}; call it per subdirectory when the tree shows nesting.
3. When the request names a pattern or extension, locate matches with {tool:filesystem.search_files}.
4. For files that look noteworthy (largest, newest, or explicitly asked about), pull metadata with {tool:filesystem.get_file_info}.
5. Synthesize: total file count, the tree, and the largest files with sizes. Never modify anything — this skill has no write access by construction.

## Output format

A short audit report: one-line summary (file count, total-ish size), the
directory tree, then a "largest files" list with exact sizes and modified
times where gathered.
