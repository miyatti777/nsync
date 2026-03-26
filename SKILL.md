# nsync — Notion Sync Skill

任意のNotionページ配下をローカルにミラーリングし、差分同期・Push・SQLiteクエリを行う汎用ツール。

## When to Use

- Notionワークスペースの一部をローカルにCloneしたいとき
- Notionページの変更をローカルに差分同期（Pull）したいとき
- ローカルで編集したMarkdownをNotionに反映（Push）したいとき
- NotionのデータベースをSQLiteとしてローカルで参照・クエリしたいとき

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
| `sync` | Yes | 差分同期（デフォルト、初回はツリー取得も自動） |
| `sync --refresh` | Yes | ページ一覧を最新化してから差分同期 |
| `sync --force` | Yes | 全ページ強制再ダウンロード |
| `sync --full` | Yes | `--refresh` + `--force`（完全再同期） |
| `sync --dry-run` | Yes | 変更検出のみ（ダウンロードしない） |
| `pull <file.md>` | Yes | 特定ページをNotionから再取得 |
| `pull --dry-run <file.md>` | Yes | Notion側ブロック一覧のプレビュー |
| `push <file.md>` | Yes | ローカルMDをNotionに反映 |
| `push --dry-run <file.md>` | No | Push内容のプレビュー（書き込みなし） |
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

## Push Quality (Markdown → Notion)

### インライン装飾

`**bold**`, `*italic*`, `~~strike~~`, `` `code` ``, `[link](url)` をNotion rich_textに変換。

### 対応構文

見出し(H1-H3), リスト, チェックボックス, コードブロック, 引用, 区切り線, 画像 (`![alt](url)`), 段落。

### コードブロック

言語エイリアス対応（`py`→`python`, `js`→`javascript` 等）。長文は1800文字単位でチャンク分割（Notion制限対応）。

### dry-run モード

```bash
./nsync.sh push --dry-run path/to/page.md
```

Notionに書き込まず、変換されるブロック一覧をプレビュー表示。

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
| `exclude_paths` | rglob検索から除外するパス文字列 | `["_sync", "_archived"]` |

## Data Model

### ページ → Markdown

YAML front matter付きで保存。本文が空のコンテナページも正常に保存される。

### データベース → SQLite

各Notion DBは個別の `.db` ファイルとして、Notionの階層に合わせた位置に配置。
各DBには `data` テーブル（全レコード）と `_metadata` テーブル（同期情報）が含まれる。

## Typical Workflows

### 日次同期
```bash
./nsync.sh                     # デフォルトで sync を実行
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
- `push` は child_page / child_database ブロックを保護（削除しない）
- 連続するリスト項目は1行改行、ブロック種別変更時は2行改行で出力
- ネストブロックは最大3階層まで再帰取得（API呼び出し増加に注意）
- PyYAMLがインストールされていない環境でも動作する簡易YAMLパーサー内蔵
- `.env` ファイルは `.gitignore` で除外済み（秘密情報は含まれない）

## License

MIT License — 詳細は [LICENSE](LICENSE) を参照。
