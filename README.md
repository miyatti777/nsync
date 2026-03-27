# nsync — Notion Sync Tool

任意のNotionページ配下をローカルにミラーリングし、差分同期・Push・SQLiteクエリを行うCLIツール。

**外部依存ゼロ**（Python 3.7+ 標準ライブラリのみ）。PyYAML はオプション。

## 特徴

- **Sync**: Notionページ配下をローカルにMarkdownとして差分同期（`last_edited_time` 比較）
- **Pull / Push**: 特定ページの個別取得・反映（インライン装飾対応）
- **Refresh**: Notionのページツリーを最新化してから同期（新規・削除ページの検出）
- **DB → SQLite**: NotionデータベースをSQLiteファイルに変換し、SQLクエリ可能
- **Claude Skill**: AIアシスタント（Claude / Cursor）から直接呼び出し可能

## インストール

### Claude Skill として使う場合（推奨）

プロジェクトルートで:

```bash
mkdir -p .claude/skills
git clone https://github.com/miyatti777/nsync.git .claude/skills/nsync
```

または `$HOME/.claude/skills/` にグローバルインストール:

```bash
mkdir -p ~/.claude/skills
git clone https://github.com/miyatti777/nsync.git ~/.claude/skills/nsync
```

### スタンドアロンで使う場合

```bash
# スクリプトを直接ダウンロード
curl -o nsync.py https://raw.githubusercontent.com/miyatti777/nsync/main/scripts/nsync.py
chmod +x nsync.py
```

## 初期セットアップ

### 1. Notion Integration の作成

1. [Notion Integrations](https://www.notion.so/my-integrations) にアクセス
2. 「新しいインテグレーション」を作成
3. 「コンテンツを読み取る」「コンテンツを更新する」権限を付与
4. トークン（`ntn_` で始まる文字列）をコピー

### 2. 対象ページにインテグレーションを接続

Notion上で同期したいページを開き、右上の `...` → 「コネクトを追加」 → 作成したインテグレーションを選択。

### 3. トークンの設定

```bash
# Skill ディレクトリに .env を作成
echo "NOTION_API_TOKEN=ntn_xxxx" > .claude/skills/nsync/.env
echo "NOTION_API_VERSION=2022-06-28" >> .claude/skills/nsync/.env
```

## Quick Start

### 1. ワークスペースの初期化

```bash
python3 .claude/skills/nsync/scripts/nsync.py init <notion-page-url> [output-dir]
```

例:
```bash
python3 .claude/skills/nsync/scripts/nsync.py init \
  "https://www.notion.so/myworkspace/My-Project-abc123def456" \
  "projects/my-project"
```

`init` は以下を自動実行:
- `.nsync.yaml` 生成（Notion APIからページタイトルを取得してlabel設定）
- `nsync.sh` 生成（ポータブルなラッパースクリプト）
- `_sync/.env` にトークンをコピー

### 2. 同期

```bash
cd projects/my-project
./nsync.sh sync     # 差分同期（初回はツリー取得も自動実行）
```

以降は `./nsync.sh` だけで差分同期が実行されます。

## コマンド一覧

| コマンド | API必要 | 説明 |
|---------|---------|------|
| `init <url> [dir]` | Yes | 新規ワークスペース作成 |
| `sync` | Yes | 差分同期（デフォルト、初回はツリー取得も自動） |
| `sync --refresh` | Yes | ページ一覧を最新化してから差分同期 |
| `sync --force` | Yes | 全ページ強制再ダウンロード |
| `sync --full` | Yes | `--refresh` + `--force`（完全再同期） |
| `sync --dry-run` | Yes | 変更検出のみ（ダウンロードしない） |
| `pull <file>` | Yes | 特定ページ/DBを再取得（.md/.db 対応） |
| `pull -r <url>` | Yes | Notion URLのサブツリーを再帰的にPull |
| `pull --dry-run <file>` | Yes | Notion側の内容プレビュー |
| `push <file>` | Yes | ローカルファイルをNotion反映（.md/.db 対応） |
| `push --dry-run <file>` | Yes | Push内容のプレビュー |
| `status` | No | 同期状態のサマリー表示 |
| `init-state` | No | 既存ファイルから同期状態を再構築 |
| `db-list` | No | SQLiteデータベース一覧 |
| `query <db> "SQL"` | No | DBにSQLクエリ実行 |

## ワークスペース構造

`init` で生成される構造:

```
<output_dir>/
├── .nsync.yaml           # 設定ファイル
├── nsync.sh              # ラッパースクリプト（自動生成）
├── _sync/
│   ├── .env              # ローカルトークン（省略可）
│   ├── tree_cache.json   # クロール結果キャッシュ
│   └── sync_state.json   # 差分同期状態
├── Page名.md             # 同期されたページ
└── DB名.db               # Notion DB → SQLite
```

## トークン管理

トークンは以下の優先順位で解決されます:

1. `NOTION_API_TOKEN` 環境変数
2. `<output_dir>/_sync/.env` — ワークスペース固有
3. `<skill_dir>/.env` — 共通トークン（通常はこれだけでOK）

## 設定 (.nsync.yaml)

```yaml
root_page_id: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
label: "My Project"
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
| `exclude_paths` | 検索から除外するパス | `["_sync", "_archived"]` |

## 対応ブロックタイプ

### Pull (Notion → Markdown)

| Notion | Markdown |
|--------|----------|
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

インライン装飾: **太字**, *イタリック*, ~~取り消し線~~, `コード`, [リンク](url)

ネストブロック: 最大3階層まで再帰取得。

### Push (Markdown → Notion)

見出し(H1-H3), リスト, チェックボックス, コードブロック, 引用, 区切り線, 画像, 段落。
インライン装飾（太字/イタリック/取り消し線/コード/リンク）対応。

`push` は `child_page` / `child_database` ブロックを保護します（削除しない）。

### DB 行のページ本文

`.nsync.yaml` に `db_page_content: true` を設定すると、データベース各行のページ本文（Notion上でレコードを開いた時に表示されるコンテンツ）を Markdown に変換し、SQLite の `_body` カラムに格納します。

```yaml
db_page_content: true
```

```sql
-- 本文にキーワードを含むレコードを検索
SELECT Name, substr(_body, 1, 200) FROM data WHERE _body LIKE '%Sprint%'
```

デフォルトは `true`。大量レコードの DB で無効にしたい場合は `db_page_content: false` を設定してください。

### Pull (単一ファイル取得)

特定のページまたは DB を Notion から再取得:

```bash
./nsync.sh pull --dry-run path/to/page.md   # ページのプレビュー
./nsync.sh pull path/to/page.md              # ページを上書き更新

./nsync.sh pull --dry-run path/to/db.db     # DB のプレビュー（行数・プロパティ一覧）
./nsync.sh pull path/to/db.db               # DB を再取得して上書き
```

`sync` が全ページの差分同期なのに対し、`pull` は指定した1ファイルだけを即座に更新します。

### Pull Recursive (サブツリー取得)

Notion URL を指定して、そのページ配下だけを再帰的にクロール＆ダウンロード:

```bash
# サブツリーだけ取得（sync --refresh よりも高速）
./nsync.sh pull -r "https://www.notion.so/xxxxx/Page-Name-xxxxx"

# プレビュー（ダウンロードしない）
./nsync.sh pull -r --dry-run "https://www.notion.so/xxxxx/Page-Name-xxxxx"
```

`sync --refresh` がルート全体を再クロールするのに対し、`pull -r` は**指定ページ以下だけ**を処理します。
Notion側でページを移動してきた場合や、特定セクションだけ更新したい場合に便利です。

- サブツリーをクロール → 既存 `tree_cache.json` にマージ
- ページと DB の両方をダウンロード
- `sync_state` も更新（次回 `sync` で二重ダウンロードされない）

### DB Push (SQLite → Notion)

SQLite の行データを Notion DB に反映:

```bash
./nsync.sh push --dry-run path/to/db.db    # プレビュー（更新/新規件数を表示）
./nsync.sh push path/to/db.db              # 実行
```

- `_notion_page_id` がある行 → プロパティ値を更新（PATCH）
- `_notion_page_id` が空の行 → 新規行として作成（POST）
- 対応型: title, rich_text, number, select, multi_select, date, checkbox, url, status, email, phone_number
- 読み取り専用プロパティ（formula, rollup, relation 等）は自動スキップ

## nsync.sh の発見ロジック

`init` で生成される `nsync.sh` は、以下の順序で `nsync.py` を探します:

1. `NSYNC_SCRIPT` 環境変数（明示指定用）
2. Git ルートの `.claude/skills/nsync/scripts/nsync.py`
3. `$HOME/.claude/skills/nsync/scripts/nsync.py`

特殊な配置の場合は `NSYNC_SCRIPT` 環境変数で指定してください:

```bash
export NSYNC_SCRIPT=/path/to/nsync.py
./nsync.sh sync
```

## 動作要件

- Python 3.7+
- Notion API トークン（Internal Integration）
- PyYAML（オプション、なくても簡易パーサーで動作）

## クロールの再開

大規模なNotionツリー（数百ページ以上）のクロールは時間がかかることがあります。
nsync はクロール中に50件ごとにチェックポイントを `_sync/crawl_checkpoint.json` に保存します。

プロセスが中断した場合（タイムアウト、ネットワークエラーなど）、次回 `sync` 実行時にチェックポイントから自動的に再開します。

```bash
# 中断後に再実行 → 自動で途中から再開
./nsync.sh sync
# Found checkpoint (2026-03-26T23:46:51): 150 items, 134 queue
# Resuming from checkpoint: 150 items, 60 queue
```

`crawl_max_depth` を変更した場合、不要なキュー項目は自動的にスキップされます。

## Rate Limit ハンドリング

Notion API のレート制限（429）に対して:

- **Retry-After 対応**: Notion が返す `Retry-After` ヘッダーの秒数で待機（ヘッダーがない場合は指数バックオフ）
- **安全停止**: 連続して 429 が返り続けた場合、チェックポイントを保存して安全に停止（exit code 2）
- **自動再開**: 次回実行時にチェックポイントから再開
- **API 統計**: プロセス終了時に `[API: 500 calls, 3 rate-limited, 1 errors]` のようなサマリーを表示

```bash
# Rate Limit で停止した場合、再実行するだけで再開
./nsync.sh sync
# Found checkpoint: 300 items, 200 queue → 自動再開
```

## ライセンス

MIT License — 詳細は [LICENSE](LICENSE) を参照。
