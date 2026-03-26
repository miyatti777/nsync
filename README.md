# nsync — Notion Sync Tool

任意のNotionページ配下をローカルにミラーリングし、差分同期・Push・SQLiteクエリを行うCLIツール。

**外部依存ゼロ**（Python 3.7+ 標準ライブラリのみ）。PyYAML はオプション。

## 特徴

- **Clone**: Notionページ配下のツリーをローカルにMarkdownとしてダウンロード
- **差分 Sync**: `last_edited_time` を比較し、変更があったページだけを再取得
- **Push**: ローカルで編集したMarkdownをNotionに反映（インライン装飾対応）
- **DB → SQLite**: NotionデータベースをSQLiteファイルに変換し、SQLクエリ可能
- **Claude Skill**: AIアシスタント（Claude / Cursor）から直接呼び出し可能

## インストール

### Claude Skill として使う場合（推奨）

プロジェクトルートで:

```bash
mkdir -p .claude/skills
git clone https://github.com/your-org/nsync.git .claude/skills/nsync
```

または `$HOME/.claude/skills/` にグローバルインストール:

```bash
mkdir -p ~/.claude/skills
git clone https://github.com/your-org/nsync.git ~/.claude/skills/nsync
```

### スタンドアロンで使う場合

```bash
# スクリプトを直接ダウンロード
curl -o nsync.py https://raw.githubusercontent.com/your-org/nsync/main/scripts/nsync.py
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

### 2. クロール & 同期

```bash
cd projects/my-project
./nsync.sh crawl    # ツリー構造を取得
./nsync.sh sync     # 差分同期
```

以降は `./nsync.sh` だけで差分同期が実行されます。

## コマンド一覧

| コマンド | API必要 | 説明 |
|---------|---------|------|
| `init <url> [dir]` | Yes | 新規ワークスペース作成 |
| `crawl` | Yes | Notion側のツリーを再クロール |
| `sync` | Yes | 差分同期（デフォルト） |
| `sync --force` | Yes | 全ページ強制再ダウンロード |
| `full` | Yes | `crawl` + `sync --force` |
| `push <file.md>` | Yes | ローカルMDをNotionに反映 |
| `push --dry-run <file.md>` | No | Push内容のプレビュー |
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

## ライセンス

MIT License — 詳細は [LICENSE](LICENSE) を参照。
