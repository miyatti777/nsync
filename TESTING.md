# nsync テストガイド

## 自動テスト（API不要・依存ゼロ）

```bash
python3 -m unittest discover tests        # リポジトリルートから
# pytest導入済み環境なら: python3 -m pytest tests
```

`tests/test_smoke.py` は Notion API・トークン・ネットワークなしで動く純粋関数のスモークテスト。
カバー範囲: front matter引用符 / インラインリンク（アンカー・山括弧・tree_cache解決・曖昧タイトル）/
インライン装飾（bold等+`<span color>`）/ `<callout>`・`<details>` のパースとエミット /
旧形式後方互換（quote・リスト）/ push -r 子リンク検出漏れ警告。

## 手動チェックリスト（API必要・テスト専用ページで実施）

**前提:** 稼働中ワークスペースでは行わない。テスト専用のNotionページ配下で `init` して実施する。
削除を伴うため、実施後にテストページを片付けること。

### 1. 基本round-trip

- [ ] `init <テストページURL> <dir>` → `.nsync.yaml` / `nsync.sh` / `_nsync/` が生成される
- [ ] `sync` 初回実行 → ページ階層がローカルにミラーされる
- [ ] ローカルMDを1行編集 → `sync` → Notionに反映され、他ページは再DLされない（差分同期）
- [ ] `sync --dry-run` → 変更検出のみで書き込みなし

### 2. 装飾round-trip（2026-07追加）

- [ ] Notionでcallout（絵文字icon+背景色）を作成 → `pull` → ローカルMDに `<callout icon=".." color="..">` が出る
- [ ] そのまま `push` → Notion側でcalloutのまま（**quoteに劣化しない**）。icon・背景色維持
- [ ] toggle（中にリスト・コードブロック）→ `pull` → `<details><summary>` + タブ字下げ → `push` → toggleと中の構造が維持される
- [ ] 文字色・背景色付きテキスト → `pull` → `<span color="..">` → `push` → 色維持
- [ ] **2往復チェック**: `pull` → `push` → `pull` でMDが変化しない（ドリフトなし）
- [ ] 旧形式の確認: `> 絵文字 テキスト` を含むMDを `push` → quoteとして送られる（勝手にcallout化しない）

### 3. 新規ページ作成 / push -r

- [ ] `new "親" "タイトル" --children "A,B"` → scaffold生成 → `push --dry-run -r` → 構造検証OK → `push -r` → 親+子が作成され `notion_id` が書き戻される
- [ ] 子リンクをアイコンなし形式 `[B](B/B.md)` にして `push -r` → `⚠ NOT A CHILD LINK` 警告が出る
- [ ] front matterの `notion_parent` を引用符付きで書いて `push` → 成功する（引用符は自動除去）

### 4. リンク処理

- [ ] 本文にアンカーリンク `[x](#見出し)` → `push` 成功（テキスト化ログが出る）
- [ ] スペース入りファイル名への山括弧リンク `[x](<My Doc.md>)` → 同期済みページならNotionリンクに解決、なければ `x (My Doc.md)` にテキスト化
- [ ] `push --dry-run` で上記の変換予定が表示される

### 5. メディア・DB

- [ ] 画像付きページ `pull` → `_assets/` にDL、`push` → File Upload APIで再UL
- [ ] Notion DB → `.db` 生成、`query <db> "SELECT ..."` が動く
- [ ] `.db` の行を編集して `push <db>` → Notionのプロパティに反映

### 6. 耐障害性

- [ ] 大きいツリーの `sync` 中に Ctrl+C → 再実行でチェックポイントから再開
- [ ] 引用符付きUUID等のvalidationエラー → 4xxは即失敗しリトライしない（ログに3回連続同エラーが出ない）
