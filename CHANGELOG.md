# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Codex skill support** — `install --target codex` installs nsync into
  `.agents/skills/nsync`, `SKILL.md` includes Codex-compatible front matter,
  and generated `nsync.sh` wrappers discover both Codex and Claude/Cursor
  canonical skill locations.

## [1.1.1] - 2026-07-15

Fixes three Notion↔local sync gaps found in real use — URL parsing, push
container nesting, front-matter preservation on pull — plus rename-aware
custom-key carry for `pull -r`.

実運用で見つかった3件の同期の取りこぼし（URLパーサ・push時の親コンテナ生成・
pull時のfront matter保持）に加え、`pull -r` のrename追随を修正。

### Fixed

- **URL parsing for `app.notion.com/p/` links** — `pull -r` (and any URL-taking
  command) now extracts the page ID from `app.notion.com/p/<ws>/<slug>-<id>`
  links including `?t=` query and `#anchor`, not just `www.notion.so`. Raw IDs
  and uppercase hex are also accepted. (`app.notion.com/p/` 形式・クエリ/アンカー付き
  URL・生ID・大文字hex に対応)
- **Push no longer flattens new subfolders** — pushing a file under an
  intermediate folder with no Notion page (e.g. `Agents/`, `Documents/`) now
  auto-creates a container page under the nearest existing ancestor and nests
  the file below it, instead of scattering everything onto the layer top.
  Existing containers are reused; `push --dry-run` shows `CREATE container: <name>`
  and no longer persists an inferred `notion_parent` (which would have poisoned
  a later real push). (中間フォルダのコンテナページを自動生成・既存は再利用・
  dry-run表示・dry-runはfront matterを汚染しない)
- **Pull preserves custom front matter** — `pull` / `pull -r` now merge only the
  nsync-managed keys and keep user-defined keys (e.g. subagent `name` /
  `description` / `tools` / `delegable`) instead of overwriting the whole header.
  (pull往復でカスタムfront matterキーを温存)
- **`pull -r` follows Notion-side renames** — recursive pull now runs rename
  detection first, moving the old local file (with its custom keys) to the new
  path before downloading, instead of leaving an orphan and dropping the keys.
  (pull -r がrename/移動を検出し、カスタムキーごと新パスへ引き継ぎ)

### Added

- `tests/test_sync_gaps.py` — 27 API-free unit tests covering the four fixes
  (URL extraction, container planning/dry-run, front-matter merge, rename carry).

## [1.1.0] - 2026-07-14

Adds an `install` subcommand that places nsync in the canonical skill directory
and scaffolds `.env`, removing the manual-placement footguns behind v1.0.0's F1.

`install` サブコマンドを追加。正準スキルディレクトリへの配置と `.env` 雛形生成を
1コマンド化し、手動配置起因の不具合（v1.0.0 の F1）を解消します。

### Added

- **`install [--target claude|cursor|global] [--dir PATH] [--force]`** — places
  nsync into the canonical skill directory (`.claude/skills/nsync`, shared by
  Claude Code and Cursor) and scaffolds a `.env` template in one step. Safety by
  design:
  - never overwrites an existing `.env` (token safety)
  - refuses to overwrite a non-empty target without `--force`
  - idempotent — running from the canonical path only ensures `.env`, no copy
  - copies an allowlist only (never carries over `.env`, `_sync/`, `__pycache__`,
    `*.db`, `.git`)
  - guards against nested source/target and a file sitting at the target path
- Regression tests for `install` (`tests/test_install.py`).

### Changed

- `__version__` corrected to track the released version (was left at `0.1.0`
  through the v1.0.0 release).

### Notes

- This release only adds the `install` command; the push / sync / pull code paths
  are unchanged from v1.0.0, so the v1.0.0 destructive-behavior guarantees (child
  protection, mention preservation, link safety) carry over unchanged.
- F1 (non-standard install locations needing `NSYNC_SCRIPT`) from the v1.0.0 E2E
  findings is now addressed by `install` canonicalizing placement.

[1.1.0]: https://github.com/miyatti777/nsync/releases/tag/v1.1.0

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
