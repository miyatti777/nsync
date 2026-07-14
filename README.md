# nsync — Notion Sync Tool

任意のNotionページ配下をローカルにミラーリングし、差分同期・Push・SQLiteクエリを行うCLIツール。

**外部依存ゼロ**（Python 3.7+ 標準ライブラリのみ）。PyYAML はオプション。

## 特徴

- **Bidirectional Sync**: ローカル変更を自動検出→Push後、Notion側の変更をPull（コンテンツハッシュで正確な差分検出）
- **Pull / Push**: 特定ページの個別取得・反映（インライン装飾対応）
- **Media Support**: 画像/PDF/動画/音声のダウンロード＆アップロード（Notion File Upload API対応）
- **Refresh**: Notionのページツリーを最新化してから同期（新規・削除ページの検出）
- **DB → SQLite**: NotionデータベースをSQLiteファイルに変換し、SQLクエリ可能
- **New Page Creation**: ローカルでページ構造を作成→Notionに一括反映（scaffold + recursive push）
- **Claude Skill**: AIアシスタント（Claude / Cursor）から直接呼び出し可能

> 📖 **運用ガイド:** [Notion × Claude Code 往復ワークフロー](docs/roundtrip-workflow.md) — 戦略・企画は Notion、実装・執筆は Claude Code で行き来する循環ワークフローの方法論

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

> Claude Code / Cursor は配置先が同じ（`.claude/skills/`）です。専用のスクリプトは不要で、SKILL.md が入った上記の配置だけで自然言語から呼び出せます。

### `install` で配置を正準化する

clone 後に `install` を実行すると、**正準パス（`.claude/skills/nsync`）への配置**と **`.env` 雛形の生成**をまとめて行えます。非標準の場所にクローンしてしまったときの「`nsync.sh` が `nsync.py` を見つけられない」落とし穴（後述の発見ロジック参照）を防げます。

```bash
python3 <cloneした場所>/scripts/nsync.py install            # git ルート配下の .claude/skills/nsync に配置
python3 <cloneした場所>/scripts/nsync.py install --target global   # $HOME/.claude/skills/nsync に配置
python3 <cloneした場所>/scripts/nsync.py install --dir /path/to/proj  # 配置先のベースディレクトリを明示
```

安全設計（人の操作・上書き起因の不具合を防ぐ）:

- 既存の `.env` は**絶対に上書きしません**（トークンを守る）
- 既にインストール済みの場所は、`--force` なしでは**上書きを拒否**します（`--force` で更新）
- **冪等** — 正準パスで実行した場合は配置をコピーせず `.env` 雛形の確認のみ
- コピー対象は許可リスト（`scripts/` `docs/` `SKILL.md` など）のみ。`.env` / `_sync/` / `__pycache__` などは持ち込みません

`install` はローカルへの配置だけを行います。トークン取得と Notion ページへの「コネクトを追加」は Notion 側の手作業として残ります（下記「初期セットアップ」）。

### スタンドアロンで使う場合

```bash
# スクリプトを直接ダウンロード
curl -o nsync.py https://raw.githubusercontent.com/miyatti777/nsync/main/scripts/nsync.py
chmod +x nsync.py
```

> ⚠️ 標準配置（Gitルートや `$HOME` の `.claude/skills/nsync`）以外に置いた場合、`init` が生成する `nsync.sh` から `nsync.py` を自動発見できないことがあります。その場合は `NSYNC_SCRIPT` 環境変数でパスを指定してください（「nsync.sh の発見ロジック」参照）。

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
- `_nsync/.env` にトークンをコピー

### 2. 同期

```bash
cd projects/my-project
./nsync.sh sync     # 差分同期（初回はツリー取得も自動実行）
```

以降は `./nsync.sh` だけで差分同期が実行されます。

## コマンド一覧

| コマンド | API必要 | 説明 |
|---------|---------|------|
| `install [--target claude\|cursor\|global] [--dir PATH] [--force]` | No | 正準パスへ配置＋`.env`雛形生成（冪等・既存.env非上書き） |
| `init <url> [dir]` | Yes | 新規ワークスペース作成 |
| `sync` | Yes | 双方向同期（ローカル変更Push→リモート変更Pull） |
| `sync --refresh` | Yes | ページ一覧を最新化してから同期 |
| `sync --force` | Yes | 全ページ強制再ダウンロード |
| `sync --full` | Yes | `--refresh` + `--force`（完全再同期） |
| `sync --dry-run` | Yes | 変更検出のみ（ダウンロードしない） |
| `sync --no-push` | Yes | ローカル変更の自動Pushをスキップ |
| `pull <file>` | Yes | 特定ページ/DBを再取得（.md/.db 対応） |
| `pull -r <url>` | Yes | Notion URLのサブツリーを再帰的にPull |
| `pull --dry-run <file>` | Yes | Notion側の内容プレビュー |
| `push <file>` | Yes | ローカルファイルをNotion反映（.md/.db 対応） |
| `push -r <file>` | Yes | 再帰Push（子ページも作成・更新。子リンクは `[📄 タイトル](パス)` 単独行形式が必須） |
| `push --dry-run <file>` | Yes | Push内容のプレビュー（`-r` 併用時は構造検証付き） |
| `new <parent> "Title"` | No | 新規ページのローカル構造を生成（scaffold） |
| `status` | No | 同期状態のサマリー表示 |
| `init-state` | No | 既存ファイルから同期状態を再構築 |
| `db-list` | No | SQLiteデータベース一覧 |
| `query <db> "SQL"` | No | DBにSQLクエリ実行 |

## ⚠️ Safety — 安全に使う

nsync は子ページ保護・競合スキップ・dry-run などの安全機構を備えています。この節では「push が何を置き換えるか」と、より安全に使うための作法をまとめます。

### push は何を置き換えるか

`push <file>` は、ローカル Markdown の内容で Notion ページの本文を**全置換**します（部分マージではありません）。ローカルで削除した段落は Notion 側でも削除されます。

一方、以下は自動で保護されます:

- **子ページ・子データベース** — push 時に `child_page` / `child_database` を自動検出し、子を削除しない安全な経路（block API）で反映します（過去に子ページがゴミ箱に落ちる不具合がありましたが修正済み）
- **削除許可の無効化** — 子が検出されなかった場合の Markdown API 経路でも `allow_deleting_content: false` で送信し、検出漏れがあっても子コンテンツを消さない二重ガードになっています

### 推奨フロー: dry-run → push

```bash
./nsync.sh push --dry-run page.md   # 何が送られるかプレビュー（-r 併用時は構造検証付き）
./nsync.sh push page.md             # 問題なければ実行
```

`sync` も同様に `sync --dry-run` で変更検出だけを先に確認できます。

### 競合（CONFLICT）時の挙動と解決

同じページがローカルと Notion の両方で変更されていた場合、`sync` はそのファイルを**スキップして警告**します（どちらの変更も上書きしません）:

```
CONFLICT: ページ名 (both local & remote changed, skipping)
```

解決方法:

| 優先したい側 | コマンド | 補足 |
|------------|---------|------|
| ローカル版 | `./nsync.sh push <file>` | これだけで解消（次回 sync から通常動作） |
| Notion 版 | `./nsync.sh pull <file>` → `./nsync.sh init-state` | **`init-state` まで実行してください。** 単発 pull は同期状態を更新しないため、pull だけでは次回以降の sync でも CONFLICT 扱いが続きます |

> ⚠️ `init-state` は同期状態をローカルファイルの現状から作り直します。実行前に**他ファイルに未 Push のローカル編集がないこと**を確認してください（未 Push 編集は「変更なし」として基準化され、次回 sync の再ダウンロードで上書きされます）。また `init-state` 直後の sync は全ページの再取得が走ります。

### 既知の注意点

- **toggle は「別行形式」で書く** — `<details>` と `<summary>見出し</summary>` をそれぞれ独立した行に書いてください。1行にまとめると正しく復元されません（見出しテキストが失われた空トグルになる、または平文の段落として Push されます）

  ```markdown
  <details>
  <summary>見出し</summary>
  	内容（タブ字下げ）
  </details>
  ```

- **Notion ページへの参照は URL 形式 or 子リンク形式で** — `https://www.notion.so/...` の URL 形式、または単独行の `[📄 タイトル](相対パス)` 形式を使ってください。通常の相対パスリンク（`[タイトル](../page.md)` 等）はそのまま送信され、Notion 上ではページ参照（mention）になりません
- **`--legacy` push の副作用** — block API を強制する `--legacy` では、保持されたトグルがページ先頭に移動します（API 制約による既知の制限）。Notion 上で手動ドラッグで位置を戻してください
- **子ページリンクの出力形式が経路で異なる** — `sync` / `pull -r`（block API 経路）は `[📄 タイトル](相対パス)` 形式、単発 `pull`（Markdown API 経路）は `<page url="...">タイトル</page>` タグで出力します。`push -r` の子ページ検出は 📄 形式のみが対象です

### 復旧手段

万一意図しない内容を push してしまっても、復元できます:

- **Notion ページ履歴から復元** — ページ右上の `...` → 「ページ履歴」から過去バージョンに戻せます
- **ローカル側は git 管理を推奨** — ワークスペースを git 管理下に置けば、ローカル Markdown も任意の時点に戻せます

## ワークスペース構造

`init` で生成される構造:

```
<output_dir>/
├── .nsync.yaml           # 設定ファイル
├── nsync.sh              # ラッパースクリプト（自動生成）
├── _nsync/                # nsync専用（csyncの _csync/ と共存可能）
│   ├── .env              # ローカルトークン（省略可）
│   ├── tree_cache.json   # クロール結果キャッシュ
│   └── sync_state.json   # 差分同期状態
├── Page名/
│   ├── Page名.md         # 同期されたページ
│   └── _assets/          # ダウンロードした画像・メディア
├── DB名.db               # Notion DB → SQLite
└── _db_assets/           # DB files プロパティのダウンロード
```

## トークン管理

トークンは以下の優先順位で解決されます:

1. `NOTION_API_TOKEN` 環境変数
2. `<output_dir>/_nsync/.env` — ワークスペース固有
3. `<skill_dir>/.env` — 共通トークン（通常はこれだけでOK）

## 設定 (.nsync.yaml)

```yaml
root_page_id: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
label: "My Project"
crawl_max_depth: 10
rate_limit_delay: 0.35
exclude_paths:
  - "_nsync"
  - "_csync"
  - "_archived"
  - "_sync"
```

| Key | 説明 | デフォルト |
|-----|------|-----------|
| `root_page_id` | 同期対象のNotionルートページID | (必須) |
| `label` | 表示用ラベル | Notionタイトル or ディレクトリ名 |
| `crawl_max_depth` | クロールの最大深度 | `10` |
| `rate_limit_delay` | API呼び出し間の待機秒数 | `0.35` |
| `db_page_content` | DB各行のページ本文を `_body` カラムに格納 | `true` |
| `exclude_paths` | 検索から除外するパス | `["_nsync", "_csync", "_archived", "_sync"]` |

## AI アシスタントからの使い方

### Cursor IDE（Claude Skill として）

Cursorのチャットで自然言語で依頼できます:

```
「NotionのMy Projectページをローカルに同期して」
→ nsync init + sync を実行

「Notion同期して」「nsync sync」
→ 双方向同期を実行

「このページをNotionに反映して」
→ push を実行

「Product Backlogの内容を見せて」
→ query コマンドでSQLite検索

「Notionの Meeting Notes だけ更新して」
→ pull -r でサブツリー同期
```

### Claude Code（ターミナル）

```bash
# Claude Code のプロンプトから
claude "nsync で My Project を同期して"
claude "Product Backlog から優先度高のタスクを検索して"
```

### CLI（直接実行）

```bash
cd projects/my-project
./nsync.sh sync              # 双方向同期
./nsync.sh push page.md      # ローカル→Notion
./nsync.sh pull page.md      # Notion→ローカル
./nsync.sh query backlog "SELECT * FROM data WHERE Status='In Progress'"
```

## 新規ページ作成

ローカルでページを作成し、Notionに新規ページとして反映できます。

### 1. Scaffold（ローカル構造の生成）

```bash
# 子ページなし
./nsync.sh new "https://www.notion.so/xxx/Parent-xxx" "企画書"

# 子ページ付き
./nsync.sh new "https://www.notion.so/xxx/Parent-xxx" "企画書" --children "調査メモ,スケジュール,議事録"

# 親をtree_cacheのページ名で指定することも可能
./nsync.sh new "My Project" "新ページ"
```

生成される構造（**親ページのフォルダ配下** = sync が使う正準パスに生成されます）:
```
親ページ名/                  ← 親がルートの場合はワークスペース直下
└── 企画書/
    ├── 企画書.md          ← notion_parent 付き front matter
    ├── 調査メモ/
    │   └── 調査メモ.md
    ├── スケジュール/
    │   └── スケジュール.md
    └── 議事録/
        └── 議事録.md
```

- 親が tree_cache に見つからない場合はワークスペース直下に生成し警告を出します（先に `./nsync.sh refresh` を実行すると親フォルダ配下に生成されます）
- 同じ `notion_id` を持つファイルが複数あると、`sync` が警告を出し、追跡対象でない方の `push` はエラーで中断します（古いコピーによる上書き事故の防止）

### 2. 編集

生成された `.md` ファイルを自由に編集。子ページリンクは自動的に挿入済み。

### 3. Notion に Push

```bash
# 親ページと子ページを一括作成
./nsync.sh push --recursive 企画書/企画書.md

# プレビュー（構造検証付き）
./nsync.sh push --dry-run --recursive 企画書/企画書.md
```

- `notion_id` がない → 新規ページとして作成（`notion_parent` に基づく）
- `notion_parent` もない → **ディレクトリ構造から親ページを自動推定**
- ファイル名から自動でタイトルを設定（front matter 不要）
- 作成後、`notion_id` が自動的に front matter に書き込まれる
- 同名ページが既に Notion にある場合は警告してスキップ
- `--recursive` で子ページリンク先も再帰的に作成

### Front Matter

`nsync new` で生成される front matter:

```yaml
---
notion_parent: 12345678-1234-1234-1234-1234567890ab
title: 企画書
---
```

| Key | 説明 |
|-----|------|
| `notion_parent` | 親ページの Notion ID（新規作成時に必要） |
| `title` | ページタイトル（新規作成時に必要） |
| `notion_id` | Notion ページ ID（作成後に自動設定） |

UUIDは引用符あり（`"..."` / `'...'`）でも可（値の先頭・末尾が同じ引用符の場合に自動除去）。

### Front Matter なしで Push

ワークスペース内のファイルであれば、front matter がなくてもそのまま Push できます:

```bash
# 人間が普通に作ったファイル（front matter なし）
echo "## 議事録 2026-03-27\n\n- 議題A\n- 議題B" > "Meeting Notes/議事録.md"

# そのまま Push → ディレクトリから親を自動推定、ファイル名をタイトルに
./nsync.sh push "Meeting Notes/議事録.md"
# → 親: Meeting Notes ページ、タイトル: 議事録
```

Push後、`notion_id` が自動的に front matter に書き込まれます。

## 子ページリンク

Pull時、Notionの子ページ・子データベースはMarkdownの相対リンクとして出力されます（`sync` / `pull -r` の場合。単発 `pull` は出力形式が異なります — 「⚠️ Safety」の既知の注意点を参照）:

```markdown
## 仕様

[📄 1_sense：リサーチ](1_sense：リサーチ/1_sense：リサーチ.md)
[📄 2_focus：戦略](2_focus：戦略/2_focus：戦略.md)
[🗃️ Product Backlog](Product_Backlog.db)
```

- **Cmd+クリック** でファイルに直接ジャンプ（VS Code/Cursor、拡張不要）
- スペースや括弧を含むパスは `<>` で囲まれ、正しくリンク動作
- Push時にリンク位置を解析し、Notion側の子ブロック順序を正確に復元

## 対応ブロックタイプ

### Pull (Notion → Markdown)

| Notion | Markdown |
|--------|----------|
| heading_1/2/3/4 | `#` / `##` / `###` / `####` |
| paragraph | テキスト（装飾付き） |
| bulleted_list_item | `- text` |
| numbered_list_item | `1. text` |
| to_do | `- [x]` / `- [ ]` |
| code | フェンスドコードブロック |
| quote | `> text` |
| callout | `<callout icon="🏷️" color="orange_bg">` 〜 `</callout>`（icon・背景色を保持、Push時に完全復元） |
| toggle | `<details>` / `<summary>見出し</summary>` を別行で記述 〜 `</details>`（子ブロックをタブ字下げで内包、Push時に復元。1行にまとめた形式は不可 — 「⚠️ Safety」の既知の注意点を参照） |
| 色付きテキスト | `<span color="red">text</span>`（文字色・背景色を保持、Push時に復元） |
| divider | `---` |
| image | `![caption](_assets/file.png)` — 内部画像は自動ダウンロード |
| pdf | `[📎 PDF名](url or _assets/file.pdf)` |
| video | `[🎬 Video名](url or _assets/file.mp4)` |
| audio | `[🔊 Audio名](url or _assets/file.mp3)` |
| file | `[📁 File名](url or _assets/file.bin)` |
| bookmark | `[title](url)` |
| embed | `[embed](url)` |
| equation | `$$expression$$` |
| table | パイプ区切りテーブル |

インライン装飾: **太字**, *イタリック*, ~~取り消し線~~, `コード`, [リンク](url)

ネストブロック: 最大3階層まで再帰取得。

### Push (Markdown → Notion)

見出し(H1-H6, H5/H6はH4に変換), リスト, チェックボックス, コードブロック, 引用, 区切り線, 画像, メディア, 段落。
インライン装飾（太字/イタリック/取り消し線/コード/リンク）対応。

**ローカルファイルのアップロード**: `_assets/` 内の画像やPDF等はNotion File Upload API（v2026-03-11）で自動アップロードされます（≤20MB、single_part）。外部URLはそのまま `external` ブロックとして処理されます。

`push` は `child_page` / `child_database` ブロックを保護し、子リンクの位置情報を使って元の構造を復元します（Position-aware Push）。

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

### マルチデータソースDB

Notion の「マルチデータソースDB」（複数のデータベースを統合したビュー）にも自動対応しています。

- 通常の API (v2022-06-28) でクエリ失敗時に自動検出
- v2025-09-03 の `data_sources` API にフォールバックして全子データソースを取得
- `_data_source` 列でデータソース元を区別可能
- `_metadata` テーブルに `multi_data_source: true` が記録される
- 設定変更は不要（自動フォールバック）

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

## 画像・メディアファイル対応

### Pull 時の動作

Notion ページ内の画像・PDF・動画・音声はローカルに自動ダウンロードされます:

```
Page名/
├── Page名.md                     ← ![caption](_assets/abc123_photo.png)
└── _assets/
    ├── abc123_photo.png          ← Notion内部画像（S3 URL失効問題を解決）
    └── def456_document.pdf       ← PDFファイル
```

- **内部画像**（Notion にアップロードされたもの）→ `_assets/` にダウンロード、相対パスで参照
- **外部画像**（外部URL参照）→ URL のまま（変更なし）
- **PDF/動画/音声/ファイル** → アイコン付きリンクとして出力: `[📎 PDF名](_assets/file.pdf)`
- DB の files プロパティ → `_db_assets/` にダウンロード

### Push 時の動作

ローカルファイルは Notion File Upload API で自動アップロード:

```bash
# _assets/ 内の画像やPDFを含むページをPush
./nsync.sh push page.md
# → ローカル画像/PDFを自動でNotionにアップロード
```

- `![alt](_assets/file.png)` → File Upload API で画像ブロックとして配置
- `[📎 PDF](_assets/file.pdf)` → File Upload API で PDF ブロックとして配置
- `![alt](https://example.com/img.png)` → 外部URL参照（従来通り）
- ファイルサイズ上限: 20MB（single_part モード）

### round-trip 整合性

Push → Pull → Push のサイクルでファイルが二重化しないよう、元ファイル名ベースのキャッシュを使用:

- 同じ元ファイル名のアセットが `_assets/` にある場合、再ダウンロードをスキップ
- block_id が変わっても、元ファイル名で既存ファイルをマッチング

### 単体ファイルの自動ページ化

Push 時、ページディレクトリ内に Markdown から参照されていない画像・PDF・動画・音声ファイルがある場合、自動的に子ページを作成して Push します:

```bash
# Push 前: photo.png が Page/ にあるが MD から参照なし
Page/
├── Page.md
└── photo.png           ← 未参照

# Push 後: 自動でページ化
Page/
├── Page.md             ← [📄 photo](photo/photo.md) が追記
└── photo/
    ├── photo.md        ← 画像だけを含むページ
    └── _assets/
        └── photo.png   ← 元ファイルが移動
```

- ファイル名がページタイトルになる
- 元ファイルは子ページの `_assets/` に移動
- 親ページの MD に子リンクが自動追記
- Notion 上では子ページとして画像/PDFが表示される

## リネーム検出

Notion 側でページ名が変更された場合、`sync` 時にローカルファイル/フォルダを自動的にリネームします:

```bash
# Notion で "Meeting Notes" → "会議メモ" にリネーム
./nsync.sh sync --refresh
# → ローカルの Meeting Notes/ が 会議メモ/ に自動移動
#    front matter の notion_path も更新
```

- `sync_state` の旧パスと `tree_cache` の現パスを比較して検出
- フォルダ（コンテナページ）はフォルダごと移動
- フラットページは .md ファイルを移動
- front matter の `notion_path` も自動更新

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
nsync はクロール中に50件ごとにチェックポイントを `_nsync/crawl_checkpoint.json` に保存します。

プロセスが中断した場合（タイムアウト、ネットワークエラーなど）、次回 `sync` 実行時にチェックポイントから自動的に再開します。

```bash
# 中断後に再実行 → 自動で途中から再開
./nsync.sh sync
# Found checkpoint (2026-03-26T23:46:51): 150 items, 134 queue
# Resuming from checkpoint: 150 items, 60 queue
```

`crawl_max_depth` を変更した場合、不要なキュー項目は自動的にスキップされます。

## 双方向同期

`sync` コマンドはデフォルトで双方向同期を行います:

1. **ローカル変更検出** — SHA256ハッシュで実際の内容変更のみを検出（mtime偽陽性を排除）
2. **自動Push** — ローカルで編集されたファイルを Notion に反映
3. **競合検出** — 同じファイルがローカル・Notion 双方で変更されている場合はスキップ＆警告
4. **リモート変更Pull** — Notion 側の変更をダウンロード

```bash
# 通常の双方向同期
./nsync.sh sync

# ローカル変更Pushをスキップ（Notion→ローカルのみ）
./nsync.sh sync --no-push
```

ローカル変更検出はファイルシステムのみの操作（API呼び出しなし）で、数百ファイルでも数百ミリ秒で完了します。

競合（CONFLICT）が検出された場合は手動で解決してください:
- `push <file>` でローカル版を優先
- `pull <file>` で Notion 版を優先（**続けて `init-state` を実行** — 単発 pull は同期状態を更新しないため、pull だけでは CONFLICT 扱いが続きます）

詳細は「⚠️ Safety — 安全に使う」の競合の項を参照。

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

## リンクの扱い（Push時）

| リンク | 挙動 |
|--------|------|
| スキーム付き（`https://` 等）/ 通常の相対パス | そのまま送信 |
| 山括弧形式 `[t](<path with space.md>)` | tree_cacheでタイトル解決できればNotionページリンク化。不能なら `t (path)` にテキスト化（pushは失敗しない） |
| アンカー `[t](#見出し)` | テキスト化（Notion APIが拒否するため） |

変換はログ表示され、`push --dry-run` で事前確認できる。

## テスト

```bash
python3 -m unittest discover tests    # API・トークン不要のスモークテスト
```

API必要な手動チェックリストは [TESTING.md](TESTING.md) を参照。

## ライセンス

MIT License — 詳細は [LICENSE](LICENSE) を参照。
