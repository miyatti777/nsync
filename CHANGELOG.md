# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-07-10

First stable release — the first version verified end-to-end in a third-party
environment (fresh clone, README-only setup) with all destructive-behavior
scenarios passing.

初の安定版リリース。第三者環境（fresh clone・README手順のみ）でのE2E受け入れ検証で
破壊シナリオ全種のPassを確認した最初のバージョンです。

### Added

- **Notion Enhanced Markdown API** integration for lossless pull/push
- **Lossless decoration round-trip**: callout (icon + background color), toggle
  (`<details>`/`<summary>` multi-line form), colored text (`<span color>`) survive
  pull → edit → push without degradation
- **Media support**: image / PDF / video / audio download (`_assets/`) and upload
- **Rename detection** and standalone media file auto-page creation
- **New page scaffold** (`new`) and recursive push (`push -r`) for creating page
  trees from local Markdown
- **Safety section in README**: what `push` replaces (full-content replacement),
  recommended `--dry-run` → `push` flow, conflict (CONFLICT) resolution steps,
  `--legacy` side effects, and recovery via Notion page history
- **Roundtrip workflow guide** (`docs/roundtrip-workflow.md`): methodology for
  cycling between Notion (strategy/planning) and Claude Code (implementation)
- API-free smoke tests (`tests/`) and manual test checklist (`TESTING.md`)

### Fixed

- **Child page protection** (destructive-behavior fix): pushing a page that
  contains child pages no longer sends them to Trash — pushes are automatically
  routed through the block API when children are detected, and the Markdown API
  path additionally sends `allow_deleting_content: false` as a second guard
- **Mention preservation**: page / date / user mentions survive push instead of
  degrading to plain text
- **Bare `.md` link safety**: links like `page.md` no longer become dead links in
  Notion (converted to code spans)
- **Canonical scaffold path**: `nsync new` now scaffolds under the parent's
  canonical folder (the same path `sync` uses), preventing duplicate local files
  with the same `notion_id`; `sync` warns on duplicates and `push` from a stale
  untracked copy is rejected

### E2E acceptance findings (F1–F6) — disposition

Findings from the third-party-environment E2E acceptance test and how each was
resolved in this release:

| ID | Severity | Finding | Disposition |
| --- | --- | --- | --- |
| F1 | P3 | Non-standard install locations require `NSYNC_SCRIPT` for `nsync.sh` to find `nsync.py` | **Documented** (README install note); auto-embedding planned post-1.0.0 |
| F2 | P2 | README showed single-line toggle syntax, which degrades to literal paragraphs on push | **Fixed (docs)** — README/SKILL now show the canonical multi-line `<details>` / `<summary>` form with an explicit warning |
| F3 | P3 | Relative-path page links are not converted to mentions (sent as-is) | **Documented** (Safety section: use URL form or child-link form) |
| F4 | P2 | `new` scaffolded at workspace root, diverging from the canonical sync path and duplicating files with the same `notion_id` | **Fixed (code)** — canonical-path scaffold + duplicate detection + stale-push rejection, with regression tests |
| F5 | P2 | Resolving a CONFLICT with `pull` does not clear it (sync state not updated) | **Documented** (Safety section: run `init-state` after pull-side resolution, with a warning about its side effects); code fix considered post-1.0.0 |
| F6 | P3 | Child-page link output format differs by path (`sync`/`pull -r` = `[📄 title](path)`, single `pull` = `<page>` tag) | **Documented** (Safety section known-notes) |

[1.0.0]: https://github.com/miyatti777/nsync/releases/tag/v1.0.0
