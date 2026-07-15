# Unit tests for the 2026-07 sync-gap fixes (L-0714-2) — stdlib only, no Notion API.
#
# Run:  python3 -m unittest discover tests        (from the nsync skill root)
#
# Covers:
#   - Bug 1: extract_page_id_from_url — app.notion.com/p/ form, ?t= query, #anchor
#   - Bug 2: push container auto-creation for intermediate folders  (added by T102)
#   - Bug 3: pull front-matter merge preserving custom keys          (added by T103)

import contextlib
import importlib.util
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "nsync.py"
_spec = importlib.util.spec_from_file_location("nsync", _SCRIPT)
ns = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ns)


@contextlib.contextmanager
def _capture():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf


# Real-world page id from the reported repro (dashless -> dashed UUID).
_HEX = "284e10daafe74e7e85ff5da286acf595"
_UUID = "284e10da-afe7-4e7e-85ff-5da286acf595"


class TestExtractPageId(unittest.TestCase):
    """Bug 1: robust page-id extraction across Notion URL shapes."""

    def test_app_notion_com_p_form_with_query_and_anchor(self):
        # The form that used to fail: app.notion.com/p/<ws>/<slug>-<hex>?t=...#anchor
        url = ("https://app.notion.com/p/explaza/"
               "kan-Palma-PL-L-0714-3-%s?t=abc123def456#section" % _HEX)
        self.assertEqual(ns.extract_page_id_from_url(url), _UUID)

    def test_www_notion_so_bare_hex(self):
        self.assertEqual(
            ns.extract_page_id_from_url("https://www.notion.so/%s" % _HEX), _UUID)

    def test_www_notion_so_slug_with_query(self):
        url = "https://www.notion.so/My-Page-Title-%s?pvs=4" % _HEX
        self.assertEqual(ns.extract_page_id_from_url(url), _UUID)

    def test_raw_dashless_hex(self):
        # A bare id passed straight in must round-trip to the dashed UUID.
        self.assertEqual(ns.extract_page_id_from_url(_HEX), _UUID)

    def test_dashed_uuid_input(self):
        self.assertEqual(ns.extract_page_id_from_url(_UUID), _UUID)

    def test_uppercase_hex_normalized_to_lowercase(self):
        # A manually-uppercased id must normalize to the lowercase UUID the API expects.
        self.assertEqual(
            ns.extract_page_id_from_url("https://www.notion.so/%s" % _HEX.upper()), _UUID)

    def test_uppercase_dashed_uuid_normalized(self):
        self.assertEqual(ns.extract_page_id_from_url(_UUID.upper()), _UUID)

    def test_no_id_returns_empty(self):
        # No hex anywhere -> empty string (caller falls back to raw input).
        self.assertEqual(ns.extract_page_id_from_url("https://example.com/foo"), "")

    def test_empty_input(self):
        self.assertEqual(ns.extract_page_id_from_url(""), "")

    def test_trailing_id_preferred_over_slug_hex(self):
        # If a 32-hex-looking run appears earlier in the slug, the trailing id wins.
        stray = "a" * 32
        url = "https://www.notion.so/%s-tag-%s" % (stray, _HEX)
        self.assertEqual(ns.extract_page_id_from_url(url), _UUID)


class _WorkspaceFixture(unittest.TestCase):
    """Temp workspace: base_output_dir + sync_dir with a fake tree_cache whose
    root page IS the base dir (root children carry no path prefix)."""

    # A root layer page plus one pre-existing sub page "SubX" (root child).
    def _tree(self):
        return [
            {"type": "page", "title": "Layer", "path": "Layer", "id": "r" * 32,
             "depth": 0, "has_children": True, "is_root": True},
            {"type": "page", "title": "SubX", "path": "SubX", "id": "s" * 32,
             "depth": 1, "has_children": True},
        ]

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="nsync-cont-"))
        sync_dir = self._tmp / "_nsync"
        sync_dir.mkdir()
        (sync_dir / "tree_cache.json").write_text(json.dumps(self._tree()))
        self._old_sync = ns.CFG.sync_dir
        self._old_base = ns.CFG.base_output_dir
        self._old_tree_json = ns.CFG.tree_json
        ns.CFG.sync_dir = sync_dir
        ns.CFG.base_output_dir = self._tmp
        ns.CFG.tree_json = sync_dir / "tree_cache.json"

    def tearDown(self):
        ns.CFG.sync_dir = self._old_sync
        ns.CFG.base_output_dir = self._old_base
        ns.CFG.tree_json = self._old_tree_json
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _mkfile(self, rel):
        fp = self._tmp / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text("---\ntitle: %s\n---\n\nbody\n" % fp.stem)
        return fp


class TestContainerMissingDirs(_WorkspaceFixture):
    """Bug 2: _infer_parent_from_path reports intermediate folders lacking a Notion page."""

    def test_single_missing_dir_under_root(self):
        fp = self._mkfile("Agents/AGT_x.md")
        pid, title, missing = ns._infer_parent_from_path(fp)
        self.assertEqual(pid, "r" * 32)          # nearest ancestor = root layer
        self.assertEqual(missing, ["Agents"])    # Agents has no Notion page yet

    def test_missing_dir_under_existing_subpage(self):
        fp = self._mkfile("SubX/Agents/AGT_x.md")
        pid, title, missing = ns._infer_parent_from_path(fp)
        self.assertEqual(pid, "s" * 32)          # SubX exists -> deepest match
        self.assertEqual(missing, ["Agents"])

    def test_multiple_missing_dirs(self):
        fp = self._mkfile("NewA/Agents/AGT_x.md")
        pid, title, missing = ns._infer_parent_from_path(fp)
        self.assertEqual(pid, "r" * 32)
        self.assertEqual(missing, ["NewA", "Agents"])

    def test_file_directly_in_base_has_no_missing(self):
        fp = self._mkfile("intent.md")
        pid, title, missing = ns._infer_parent_from_path(fp)
        self.assertEqual(pid, "r" * 32)
        self.assertEqual(missing, [])

    def test_existing_container_is_reused_not_missing(self):
        # Pre-register an "Agents" container -> no longer reported as missing.
        tree = self._tree() + [
            {"type": "page", "title": "Agents", "path": "Agents", "id": "a" * 32,
             "depth": 1, "has_children": True, "is_container": True}]
        (self._tmp / "_nsync" / "tree_cache.json").write_text(json.dumps(tree))
        fp = self._mkfile("Agents/AGT_x.md")
        pid, title, missing = ns._infer_parent_from_path(fp)
        self.assertEqual(pid, "a" * 32)
        self.assertEqual(missing, [])


class TestEnsureContainerDryRun(_WorkspaceFixture):
    """Bug 2: dry-run plans containers without touching the Notion API."""

    def test_dry_run_prints_and_never_calls_api(self):
        fp = self._mkfile("Agents/AGT_x.md")

        def _boom(*a, **k):
            raise AssertionError("_create_notion_page must not be called on dry-run")

        orig = ns._create_notion_page
        ns._create_notion_page = _boom
        try:
            with _capture() as buf:
                pid, title, created = ns._ensure_parent_container(fp, dry_run=True)
        finally:
            ns._create_notion_page = orig

        self.assertEqual(created, ["Agents"])
        self.assertIn("CREATE container: Agents", buf.getvalue())

    def test_dry_run_multiple_containers_listed(self):
        fp = self._mkfile("NewA/Agents/AGT_x.md")
        orig = ns._create_notion_page
        ns._create_notion_page = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("no API on dry-run"))
        try:
            with _capture() as buf:
                _, _, created = ns._ensure_parent_container(fp, dry_run=True)
        finally:
            ns._create_notion_page = orig
        self.assertEqual(created, ["NewA", "Agents"])
        out = buf.getvalue()
        self.assertIn("CREATE container: NewA", out)
        self.assertIn("CREATE container: Agents", out)

    def test_no_missing_dirs_creates_nothing(self):
        fp = self._mkfile("SubX/doc.md")   # SubX exists
        pid, title, created = ns._ensure_parent_container(fp, dry_run=True)
        self.assertEqual(pid, "s" * 32)
        self.assertEqual(created, [])


class TestPushDryRunDoesNotPoisonFrontMatter(_WorkspaceFixture):
    """Bug 2 regression: a dry-run push must not persist an inferred notion_parent,
    otherwise the following real push would skip container creation and re-flatten."""

    def test_dry_run_push_leaves_frontmatter_untouched(self):
        fp = self._mkfile("Agents/AGT_x.md")
        before = fp.read_text()

        def _boom(*a, **k):
            raise AssertionError("dry-run must not hit the Notion API")

        orig = ns._create_notion_page
        ns._create_notion_page = _boom
        try:
            with _capture():
                ns.cmd_push(str(fp), dry_run=True)
        finally:
            ns._create_notion_page = orig

        after = fp.read_text()
        self.assertEqual(before, after)                 # file byte-identical
        fm, _ = ns.parse_front_matter(after)
        self.assertNotIn("notion_parent", fm)           # not poisoned


class TestFrontMatterMerge(unittest.TestCase):
    """Bug 3: pull merges managed keys while preserving user-defined keys."""

    def test_custom_keys_preserved_and_managed_refreshed(self):
        existing = {
            "notion_id": "old" + "0" * 29, "notion_path": "Old/Path",
            "synced_at": "2020-01-01T00:00:00", "pushed_at": "2020-01-01T00:00:00",
            "name": "agt-reviewer", "description": "reviews stuff",
            "tools": "[Read, Grep]", "delegable": "false",
        }
        merged = ns._merge_pull_front_matter(
            existing, "n" * 32, "New/Path", "2026-07-15T00:00:00")
        # user-defined keys survive
        self.assertEqual(merged["name"], "agt-reviewer")
        self.assertEqual(merged["description"], "reviews stuff")
        self.assertEqual(merged["tools"], "[Read, Grep]")
        self.assertEqual(merged["delegable"], "false")
        # managed keys refreshed
        self.assertEqual(merged["notion_id"], "n" * 32)
        self.assertEqual(merged["notion_path"], "New/Path")
        self.assertEqual(merged["synced_at"], "2026-07-15T00:00:00")
        # push-side transients dropped
        self.assertNotIn("pushed_at", merged)
        self.assertNotIn("notion_parent", merged)

    def test_new_file_gets_only_managed_keys(self):
        merged = ns._merge_pull_front_matter({}, "n" * 32, "P", "now")
        self.assertEqual(set(merged), {"notion_id", "notion_path", "synced_at"})

    def test_notion_parent_dropped_on_pull(self):
        merged = ns._merge_pull_front_matter(
            {"notion_parent": "p" * 32, "keep": "yes"}, "n" * 32, "P", "now")
        self.assertNotIn("notion_parent", merged)
        self.assertEqual(merged["keep"], "yes")

    def test_custom_key_order_preserved(self):
        existing = {"name": "a", "description": "b", "tools": "c"}
        merged = ns._merge_pull_front_matter(existing, "n" * 32, "P", "now")
        keys = list(merged)
        self.assertEqual(keys[:3], ["name", "description", "tools"])


class TestDownloadItemPreservesFrontMatter(_WorkspaceFixture):
    """Bug 3: recursive pull (download_item) must not wipe custom front matter."""

    def test_recursive_pull_keeps_custom_keys(self):
        # An existing subagent-definition file with custom keys.
        fp = self._tmp / "Agents" / "AGT_x.md"
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(
            "---\nnotion_id: old\nname: agt-reviewer\ndescription: reviews\n"
            "tools: [Read, Grep]\ndelegable: false\npushed_at: 2020-01-01T00:00:00\n"
            "---\n\nold body\n")

        item = {"type": "page", "id": "n" * 32, "path": "Agents/AGT_x", "title": "AGT_x"}
        orig_fetch = ns.fetch_page_blocks_as_text
        ns.fetch_page_blocks_as_text = lambda _id, page_dir=None: "# Fresh\n\nnew body"
        try:
            ns.download_item(item, tree=None)
        finally:
            ns.fetch_page_blocks_as_text = orig_fetch

        fm, body = ns.parse_front_matter(fp.read_text())
        # custom keys survived the round-trip
        self.assertEqual(fm["name"], "agt-reviewer")
        self.assertEqual(fm["description"], "reviews")
        self.assertEqual(fm["tools"], "[Read, Grep]")
        self.assertEqual(fm["delegable"], "false")
        # managed keys refreshed / cleaned
        self.assertEqual(fm["notion_id"], "n" * 32)
        self.assertEqual(fm["notion_path"], "Agents/AGT_x")
        self.assertIn("synced_at", fm)
        self.assertNotIn("pushed_at", fm)
        self.assertIn("new body", body)

    def test_recursive_pull_new_file_still_works(self):
        # No pre-existing file -> only the 3 managed keys (no regression).
        item = {"type": "page", "id": "m" * 32, "path": "Docs/T900", "title": "T900"}
        orig_fetch = ns.fetch_page_blocks_as_text
        ns.fetch_page_blocks_as_text = lambda _id, page_dir=None: "body"
        try:
            ns.download_item(item, tree=None)
        finally:
            ns.fetch_page_blocks_as_text = orig_fetch
        fp = self._tmp / "Docs" / "T900.md"
        fm, _ = ns.parse_front_matter(fp.read_text())
        self.assertEqual(set(fm), {"notion_id", "notion_path", "synced_at"})


class TestCmdPullMergePreservesFrontMatter(unittest.TestCase):
    """Bug 3: the single-file `pull` command preserves custom keys too (shared helper)."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="nsync-pull-"))
        self._old_base = ns.CFG.base_output_dir
        self._old_tree_json = ns.CFG.tree_json
        ns.CFG.base_output_dir = self._tmp
        ns.CFG.tree_json = self._tmp / "_nsync" / "does_not_exist.json"

    def tearDown(self):
        ns.CFG.base_output_dir = self._old_base
        ns.CFG.tree_json = self._old_tree_json
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_single_pull_keeps_custom_keys(self):
        fp = self._tmp / "AGT_x.md"
        fp.write_text(
            "---\nnotion_id: %s\nname: agt-x\ndescription: d\ntools: [Read]\n"
            "delegable: false\npushed_at: 2020-01-01T00:00:00\n---\n\nold\n" % ("n" * 32))

        orig = ns._pull_via_markdown_api
        ns._pull_via_markdown_api = lambda _id: "# Fresh\n\nnew body"
        try:
            with _capture():
                ok = ns.cmd_pull(str(fp))
        finally:
            ns._pull_via_markdown_api = orig

        self.assertTrue(ok)
        fm, body = ns.parse_front_matter(fp.read_text())
        self.assertEqual(fm["name"], "agt-x")
        self.assertEqual(fm["description"], "d")
        self.assertEqual(fm["tools"], "[Read]")
        self.assertEqual(fm["delegable"], "false")
        self.assertEqual(fm["notion_id"], "n" * 32)
        self.assertIn("synced_at", fm)
        self.assertNotIn("pushed_at", fm)
        self.assertIn("new body", body)


if __name__ == "__main__":
    unittest.main()
