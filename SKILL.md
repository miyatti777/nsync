# nsync — Notion Sync Skill

任意のNotionページ配下をローカルにミラーリングし、差分同期・Push・SQLiteクエリを行う汎用ツール。

## When to Use

- Notionワークスペースの一部をローカルにCloneしたいとき
- Notionページの変更をローカルに差分同期（Pull）したいとき
- ローカルで編集したMarkdownをNotionに反映（Push）したいとき
- NotionのデータベースをSQLiteとしてローカルで参照・クエリしたいとき

## How to Invoke（依頼の仕方）

### Cursor IDE チャットから

自然言語で依頼するだけで、適切なコマンドが実行されます:

| 依頼例 | 実行されるコマンド |
|--------|-------------------|
| 「Notion同期して」 | `sync` |
| 「Notionの最新を取得して」 | `sync --refresh` |
| 「全ページ再ダウンロードして」 | `sync --force` |
| 「このファイルをNotionに反映して」 | `push <file>` |
| 「Product Backlogを検索して」 | `query <db> "SQL..."` |
| 「Meeting Notesだけ取得して」 | `pull -r <url>` |
| 「Palmaの下に新しいページ作って」 | `new <parent> "Title"` |
| 「企画書をNotionに反映して（子ページも）」 | `push -r <file>` |

### Claude Code / CLI から

```bash
# 双方向同期
python3 <skill_dir>/scripts/nsync.py sync

# 特定ページをPush
python3 <skill_dir>/scripts/nsync.py push path/to/page.md

# DB検索
python3 <skill_dir>/scripts/nsync.py query backlog "SELECT * FROM data LIMIT 10"
```

## Architecture

```
<skill_dir>/              # .claude/skills/nsync/ (プロジェクト or $HOME)
├── SKILL.md              # このファイル
├── README.md             # 詳細ドキュメント
├── .env                  # 共通トークン（.gitignore 対象）
└── scripts/
    └── nsync.py          # 汎用ツール本体

<output_dir>/             # ターゲットごとに init で生成
├── .nsync.yaml           # 設定ファイル
├── nsync.sh              # thin wrapper (自動生成・ポータブル)
├── _sync/
│   ├── .env              # ローカルトークン（省略可、共通が使われる）
│   ├── tree_cache.json   # クロール結果キャッシュ
│   └── sync_state.json   # 差分同期状態
├── Page名.md             # 同期されたページ
└── DB名.db               # Notion DB → SQLite
```

## Token Management

トークンは以下の優先順位で解決される:

1. `NOTION_API_TOKEN` 環境変数
2. `<output_dir>/_sync/.env` — ワークスペース固有（異なるトークンが必要な場合）
3. `<skill_dir>/.env` — 共通トークン（通常はこれだけでOK）

初回セットアップ:

```bash
echo "NOTION_API_TOKEN=ntn_xxxx" > <skill_dir>/.env
echo "NOTION_API_VERSION=2022-06-28" >> <skill_dir>/.env
```

## Quick Start

### 1. 新規ワークスペースのセットアップ

```bash
python3 <skill_dir>/scripts/nsync.py init <notion-url> [output-dir]
```

`init` は以下を自動実行:
- `.nsync.yaml` 生成（Notion APIからページタイトルを取得してlabel設定）
- `nsync.sh` 生成（共通トークンへのフォールバック付き）
- `_sync/.env` にトークンをコピー（共通トークンがある場合）

### 2. 同期

```bash
cd <output_dir>
./nsync.sh sync     # 差分同期（初回はツリー取得も自動実行）
```

## Commands

| コマンド | API必要 | 説明 |
|---------|---------|------|
| `init <url> [dir]` | Yes | 新規ワークスペース作成 |
| `sync` | Yes | 双方向同期（ローカル変更Push→リモート変更Pull） |
| `sync --refresh` | Yes | ページ一覧を最新化してから同期 |
| `sync --force` | Yes | 全ページ強制再ダウンロード |
| `sync --full` | Yes | `--refresh` + `--force`（完全再同期） |
| `sync --dry-run` | Yes | 変更検出のみ（ダウンロードしない） |
| `sync --no-push` | Yes | ローカル変更の自動Pushをスキップ |
| `pull <file>` | Yes | 特定ページ/DBをNotionから再取得（.md/.db対応） |
| `pull -r <url>` | Yes | Notion URLのサブツリーを再帰的にPull |
| `pull --dry-run <file>` | Yes | Notion側の内容プレビュー |
| `push <file>` | Yes | ローカルファイルをNotionに反映（.md/.db対応） |
| `push -r <file>` | Yes | 再帰Push（子ページも作成・更新） |
| `push --dry-run <file>` | Yes* | Push内容のプレビュー＋構造検証（*md は API不要） |
| `new <parent> "Title"` | No | 新規ページ構造をローカルに生成（scaffold） |
| `status` | No | 同期状態のサマリー表示 |
| `init-state` | No | 既存ファイルから同期状態を再構築 |
| `db-list` | No | SQLiteデータベース一覧 |
| `query <db> "SQL"` | No | DBにSQLクエリ実行 |

## Pull Quality (Notion → Markdown)

### リッチテキスト対応

Notion のインライン装飾を正しくMarkdownに変換:

| Notion | Markdown |
|--------|----------|
| **Bold** | `**Bold**` |
| *Italic* | `*Italic*` |
| ~~Strikethrough~~ | `~~Strikethrough~~` |
| `Code` | `` `Code` `` |
| Link | `[text](url)` |

### 対応ブロックタイプ

| タイプ | 出力 |
|--------|------|
| heading_1/2/3 | `#` / `##` / `###` |
| paragraph | テキスト（装飾付き） |
| bulleted_list_item | `- text` |
| numbered_list_item | `1. text` |
| to_do | `- [x]` / `- [ ]` |
| code | フェンスドコードブロック |
| quote | `> text` |
| callout | `> emoji text` |
| toggle | `- text`（子ブロック展開） |
| divider | `---` |
| image | `![caption](url)` |
| bookmark | `[title](url)` |
| embed | `[embed](url)` |
| equation | `$$expression$$` |
| table | パイプ区切りテーブル |

### ネストブロック

リスト内のサブリスト・ネストされたコンテンツを再帰的に取得し、インデントで表現（最大3階層）。

## Pull (single page)

特定ページだけをNotionから再取得する。front matter の `notion_id` を使って対象を特定。

```bash
./nsync.sh pull --dry-run path/to/page.md   # プレビュー
./nsync.sh pull path/to/page.md              # 実行
```

`sync` が全ページの差分同期なのに対し、`pull` は指定した1ファイルだけを即座に更新する。
`push` と対になるコマンド。

## Pull Recursive (subtree)

Notion URL を指定して、そのページ配下のサブツリーだけを再帰的にクロール＆ダウンロードする。
`sync --refresh` がルート全体を再クロールするのに対し、`pull -r` は**対象ページ以下だけ**を処理するため高速。

```bash
./nsync.sh pull -r "https://www.notion.so/xxxxx/Page-Name-xxxxx"    # 実行
./nsync.sh pull -r --dry-run "https://www.notion.so/xxxxx/Page-Name-xxxxx"  # プレビュー
```

- サブツリーをクロールし、既存の tree_cache.json にマージ（新規追加+既存更新）
- ページとDBの両方をダウンロード
- sync_state も更新されるので、次回 `sync` で二重ダウンロードされない
- Notion側でページを移動してきた場合に特に有効

## Push Quality (Markdown → Notion)

### インライン装飾

`**bold**`, `*italic*`, `~~strike~~`, `` `code` ``, `[link](url)` をNotion rich_textに変換。

### 対応構文

見出し(H1-H3), リスト, チェックボックス, コードブロック, 引用, 区切り線, 画像 (`![alt](url)`), 段落。

### コードブロック

言語エイリアス対応（`py`→`python`, `js`→`javascript` 等）。長文は1800文字単位でチャンク分割（Notion制限対応）。

### DB Push (SQLite → Notion)

`.db` ファイルを指定すると、SQLite の行データを Notion DB に反映:
- `_notion_page_id` がある行 → プロパティ値を更新
- `_notion_page_id` が空の行 → 新規行として作成

対応プロパティ型: title, rich_text, number, select, multi_select, date, checkbox, url, status, email, phone_number。
formula, rollup, relation 等の算出系は読み取り専用のためスキップ。

### dry-run モード

```bash
./nsync.sh push --dry-run path/to/page.md   # ページ
./nsync.sh push --dry-run path/to/db.db     # DB
```

Notionに書き込まず、変更内容をプレビュー表示。

## Config (.nsync.yaml)

```yaml
root_page_id: "299b1337-01be-8077-807d-f97d164b62b3"
label: "Palma"
crawl_max_depth: 10
rate_limit_delay: 0.35
exclude_paths:
  - "_sync"
  - "_archived"
```

| Key | 説明 | デフォルト |
|-----|------|-----------|
| `root_page_id` | 同期対象のNotionルートページID | (必須) |
| `label` | 表示用ラベル | Notionタイトル or ディレクトリ名 |
| `crawl_max_depth` | クロールの最大深度 | `10` |
| `rate_limit_delay` | API呼び出し間の待機秒数 | `0.35` |
| `db_page_content` | DB各行のページ本文を `_body` カラムに格納 | `true` |
| `exclude_paths` | rglob検索から除外するパス文字列 | `["_sync", "_archived"]` |

## Data Model

### ページ → Markdown

YAML front matter付きで保存。本文が空のコンテナページも正常に保存される。

### データベース → SQLite

各Notion DBは個別の `.db` ファイルとして、Notionの階層に合わせた位置に配置。
各DBには `data` テーブル（全レコード）と `_metadata` テーブル（同期情報）が含まれる。

`db_page_content: true` を設定すると、各行のページ本文（Notion上でレコードを開いた時のコンテンツ）を
Markdownに変換して `_body` カラムに格納する。SQL で本文も含めて検索可能。

```sql
SELECT Name, _body FROM data WHERE _body LIKE '%キーワード%'
```

### マルチデータソースDB

Notionの「マルチデータソースDB」（複数DBを統合したビュー）にも自動対応。
通常のAPI(v2022-06-28)でエラーになった場合、v2025-09-03 の `data_sources` APIにフォールバックして全子データソースを取得・マージする。
`_data_source` 列でデータソースを区別可能。`_metadata` テーブルに `multi_data_source: true` が記録される。

## New Page Creation（新規ページ作成）

ローカルでページを作成し、Notionに新規ページとして反映するフロー。

### 1. Scaffold 生成

```bash
# 子ページ付き
./nsync.sh new "Parent Page" "企画書" --children "調査メモ,スケジュール,議事録"

# 子ページなし
./nsync.sh new "https://www.notion.so/xxx/Parent-xxx" "メモ"
```

親はNotion URL、ページタイトル、ローカル .md ファイルのいずれかで指定可能。
生成されるファイルには `notion_parent` と `title` がfront matterに設定される。

### 2. Notion に Push

```bash
# 構造検証（dry-run）
./nsync.sh push --dry-run -r 企画書/企画書.md

# 一括作成（親+子ページ）
./nsync.sh push -r 企画書/企画書.md
```

- `notion_id` がない → `notion_parent` に基づいて新規ページ作成
- `notion_parent` もない → ディレクトリ構造から親ページを自動推定、ファイル名からタイトル取得
- 作成後 `notion_id` をfront matterに自動書き戻し
- 同名ページがNotionに既存 → 警告してスキップ（既存IDにリンク）
- `tree_cache` と `sync_state` も自動更新

### Front Matter（新規ページ用）

```yaml
---
notion_parent: 299b1337-01be-8077-807d-f97d164b62b3  # 作成先の親ページID
title: 企画書                                          # ページタイトル
---
```

**front matter なしでもOK**: ワークスペース内のファイルなら、ディレクトリ構造から親ページを自動推定し、ファイル名をタイトルとして使用する。

```bash
# これだけで新規ページとして作成される
echo "## メモ" > Meeting Notes/議事録_2026-03-27.md
./nsync.sh push "Meeting Notes/議事録_2026-03-27.md"
# → 親: Meeting Notes ページ、タイトル: 議事録_2026-03-27
```

作成後は `notion_id` がfront matterに自動書き込まれ、通常の push/pull 対象になる。

## Child Page Links

Pull時、子ページ/子DBは相対パスのMarkdownリンクとして出力される。
VS Code/CursorでCmd+クリックでファイルに直接ジャンプ可能（拡張不要）。

```markdown
[📄 Page Title](./Page Title/Page Title.md)
[🗃️ DB Name](./DB_Name.db)
```

Push時はリンク位置を解析して、Notion側の子ブロック順序を正確に復元（Position-aware Push）。

## Typical Workflows

### 日次同期（双方向）
```bash
./nsync.sh                     # ローカル変更→Push、リモート変更→Pull
./nsync.sh sync --no-push      # Notion→ローカルのみ（Push スキップ）
```

### 新規ページ作成
```bash
# 1. ローカルにscaffold生成
./nsync.sh new "Parent Page" "新企画書" --children "調査メモ,スケジュール"

# 2. MDファイルを編集

# 3. 構造検証 → Notionに一括作成
./nsync.sh push --dry-run -r 新企画書/新企画書.md
./nsync.sh push -r 新企画書/新企画書.md
```

### Push前のプレビュー
```bash
./nsync.sh push --dry-run path/to/page.md
./nsync.sh push path/to/page.md    # 確認後に実Push
```

### 全データ再取得
```bash
./nsync.sh sync --full
```

## Notes

- Notion API のレート制限（3 req/sec）に対応済み（`rate_limit_delay` で調整可能）
- `push` は child_page / child_database を保護し、子リンク位置から構造を復元（Position-aware Push）
- `push -r` で新規ページ+子ページの一括作成に対応。同名ページがあれば警告してスキップ
- `new` でscaffold生成（フォルダ構造+front matter付きMD）
- 連続するリスト項目は1行改行、ブロック種別変更時は2行改行で出力
- ネストブロックは最大3階層まで再帰取得（API呼び出し増加に注意）
- PyYAMLがインストールされていない環境でも動作する簡易YAMLパーサー内蔵
- `.env` ファイルは `.gitignore` で除外済み（秘密情報は含まれない）
- クロールは50件ごとにチェックポイントを保存。プロセスが中断しても次回実行時に途中から再開
- Rate Limit (429) は `Retry-After` ヘッダーを尊重してリトライ。連続超過時はチェックポイント保存して安全停止
- 完了時に API 統計を表示（呼び出し数、429回数、エラー数）
- `sync` はデフォルトで双方向（ローカル変更検出→Push→リモート変更Pull）。SHA256ハッシュで正確な差分検出（mtime偽陽性を排除）
- ローカル・リモート両方で変更されたファイルは CONFLICT として安全にスキップ

## License

MIT License — 詳細は [LICENSE](LICENSE) を参照。
