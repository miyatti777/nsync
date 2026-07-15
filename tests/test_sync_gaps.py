# Tests for pull-side sync gaps — stdlib only, no Notion API required.
#
# Run:  python3 -m unittest discover tests        (from the repo root)
#  or:  python3 -m pytest tests                   (if pytest is installed)
#
# Covers the pull -r custom-frontmatter gap (L-0714-2 follow-up):
#   - _extract_custom_frontmatter drops sync keys, keeps user keys in order
#   - download_item preserves custom front matter across a re-download
#   - pull -r rename detection carries custom keys to the new path (no orphan)

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


def _quiet(fn, *args, **kwargs):
    """Run fn suppressing its console output; return its result."""
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kwargs)


class TestExtractCustomFrontmatter(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="nsync-fm-"))

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write(self, name, text):
        p = self._tmp / name
        p.write_text(text, encoding="utf-8")
        return p

    def test_keeps_custom_drops_sync_keys(self):
        p = self._write("a.md",
                        "---\nnotion_id: abc\nnotion_path: R/A\nsynced_at: t\n"
                        "pushed_at: t2\nname: researcher\ndelegable: true\n---\nbody")
        got = ns._extract_custom_frontmatter(p)
        self.assertEqual(got, {"name": "researcher", "delegable": "true"})

    def test_preserves_key_order(self):
        p = self._write("b.md",
                        "---\nname: r\ndescription: d\ntools: Read, Write\n"
                        "notion_id: x\ndelegable: false\n---\nbody")
        got = ns._extract_custom_frontmatter(p)
        self.assertEqual(list(got.keys()),
                         ["name", "description", "tools", "delegable"])

    def test_missing_file_returns_empty(self):
        self.assertEqual(ns._extract_custom_frontmatter(self._tmp / "nope.md"), {})

    def test_non_markdown_returns_empty(self):
        p = self._write("c.txt", "---\nname: r\n---\nbody")
        self.assertEqual(ns._extract_custom_frontmatter(p), {})

    def test_no_front_matter_returns_empty(self):
        p = self._write("d.md", "just a body, no front matter")
        self.assertEqual(ns._extract_custom_frontmatter(p), {})


class WorkspaceFixture(unittest.TestCase):
    """Base class that points CFG at a temp workspace and stubs the API fetch."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="nsync-pull-"))
        sync_dir = self._tmp / "_nsync"
        sync_dir.mkdir()

        self._saved = {
            "base_output_dir": ns.CFG.base_output_dir,
            "sync_dir": ns.CFG.sync_dir,
            "sync_state_json": ns.CFG.sync_state_json,
            "tree_json": ns.CFG.tree_json,
            "fetch": ns.fetch_page_blocks_as_text,
            "get_edited": ns.get_page_last_edited,
        }
        ns.CFG.base_output_dir = self._tmp
        ns.CFG.sync_dir = sync_dir
        ns.CFG.sync_state_json = sync_dir / "sync_state.json"
        ns.CFG.tree_json = sync_dir / "tree_cache.json"
        # Stub the only Notion-API calls the pull path makes.
        ns.fetch_page_blocks_as_text = lambda page_id, page_dir=None: "REMOTE BODY"
        ns.get_page_last_edited = lambda page_id: ""

    def tearDown(self):
        ns.CFG.base_output_dir = self._saved["base_output_dir"]
        ns.CFG.sync_dir = self._saved["sync_dir"]
        ns.CFG.sync_state_json = self._saved["sync_state_json"]
        ns.CFG.tree_json = self._saved["tree_json"]
        ns.fetch_page_blocks_as_text = self._saved["fetch"]
        ns.get_page_last_edited = self._saved["get_edited"]
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestDownloadItemPreservesFrontmatter(WorkspaceFixture):
    def test_custom_keys_survive_redownload_same_path(self):
        item = {"type": "page", "title": "Agent", "path": "Root/Agent",
                "id": "a" * 32}
        fp = ns.item_to_filepath(item)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(
            "---\nnotion_id: %s\nnotion_path: Root/Agent\nsynced_at: old\n"
            "name: researcher\ndescription: web research\n"
            "tools: Read, Write\ndelegable: true\n---\nlocal body" % ("a" * 32),
            encoding="utf-8")

        ok = _quiet(ns.download_item, item, tree=None)
        self.assertTrue(ok)

        fm, body = ns.parse_front_matter(fp.read_text(encoding="utf-8"))
        self.assertEqual(body, "REMOTE BODY")          # content refreshed
        self.assertEqual(fm["name"], "researcher")     # custom keys kept
        self.assertEqual(fm["description"], "web research")
        self.assertEqual(fm["tools"], "Read, Write")
        self.assertEqual(fm["delegable"], "true")
        self.assertEqual(fm["notion_path"], "Root/Agent")
        self.assertNotEqual(fm["synced_at"], "old")    # sync key regenerated

    def test_no_custom_keys_still_writes_standard_header(self):
        item = {"type": "page", "title": "Plain", "path": "Root/Plain",
                "id": "e" * 32}
        ok = _quiet(ns.download_item, item, tree=None)
        self.assertTrue(ok)
        fp = ns.item_to_filepath(item)
        fm, body = ns.parse_front_matter(fp.read_text(encoding="utf-8"))
        self.assertEqual(body, "REMOTE BODY")
        self.assertEqual(set(fm.keys()), {"notion_id", "notion_path", "synced_at"})


class TestPullRecursiveRenameCarry(WorkspaceFixture):
    def test_rename_carries_custom_frontmatter_to_new_path(self):
        page_id = "b" * 32
        old_item = {"type": "page", "title": "OldName", "path": "Root/OldName",
                    "id": page_id}
        new_item = {"type": "page", "title": "NewName", "path": "Root/NewName",
                    "id": page_id}

        # A previously synced subagent file at the OLD path with custom keys.
        old_fp = ns.item_to_filepath(old_item)
        old_fp.parent.mkdir(parents=True, exist_ok=True)
        old_fp.write_text(
            "---\nnotion_id: %s\nnotion_path: Root/OldName\nsynced_at: old\n"
            "name: researcher\ntools: Read\ndelegable: true\n---\nold body" % page_id,
            encoding="utf-8")

        # Sync state remembers the old path; the fresh tree has the new path.
        state = {"items": {page_id: {"type": "page", "title": "OldName",
                                     "path": "Root/OldName", "last_edited_time": "",
                                     "synced_at": "old"}}}
        ns.save_sync_state(state)
        tree = [new_item]

        # Phase 0: rename detection moves the old file to the new path.
        moved = _quiet(ns._detect_renames, tree, state)
        self.assertEqual(moved, 1)

        new_fp = ns.item_to_filepath(new_item)
        self.assertTrue(new_fp.exists())
        self.assertFalse(old_fp.exists())              # old orphan removed

        # Phase 4: download overwrites body but must keep the carried custom keys.
        ok = _quiet(ns.download_item, new_item, tree=None)
        self.assertTrue(ok)

        fm, body = ns.parse_front_matter(new_fp.read_text(encoding="utf-8"))
        self.assertEqual(body, "REMOTE BODY")
        self.assertEqual(fm["name"], "researcher")
        self.assertEqual(fm["tools"], "Read")
        self.assertEqual(fm["delegable"], "true")
        self.assertEqual(fm["notion_path"], "Root/NewName")


if __name__ == "__main__":
    unittest.main()
