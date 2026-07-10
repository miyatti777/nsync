# Smoke tests for nsync.py — stdlib only, no Notion API required.
#
# Run:  python3 -m unittest discover tests        (from the repo root)
#  or:  python3 -m pytest tests                   (if pytest is installed)
#
# Covers the regressions fixed in 2026-07:
#   - front matter quote stripping
#   - inline link handling (anchors, angle-bracket paths, tree_cache resolution)
#   - callout / toggle / span decoration round-trip syntax
#   - push -r undetected child-link warnings

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
    """Run fn suppressing its console warnings; return its result."""
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kwargs)


class TreeCacheFixture(unittest.TestCase):
    """Base class providing a temp workspace with a fake tree_cache."""

    TREE = [
        {"type": "page", "title": "ExistingPage", "path": "Root/ExistingPage",
         "id": "a" * 32, "depth": 1, "has_children": False},
        {"type": "page", "title": "DupTitle", "path": "FolderA/DupTitle",
         "id": "b" * 32, "depth": 1, "has_children": False},
        {"type": "page", "title": "DupTitle", "path": "FolderB/DupTitle",
         "id": "c" * 32, "depth": 1, "has_children": False},
    ]

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="nsync-test-"))
        sync_dir = self._tmp / "_nsync"
        sync_dir.mkdir()
        (sync_dir / "tree_cache.json").write_text(json.dumps(self.TREE))
        self._old_sync_dir = ns.CFG.sync_dir
        ns.CFG.sync_dir = sync_dir

    def tearDown(self):
        ns.CFG.sync_dir = self._old_sync_dir
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestFrontMatter(unittest.TestCase):
    def test_unquoted_value(self):
        fm, body = ns.parse_front_matter("---\nnotion_parent: abc-123\n---\nbody")
        self.assertEqual(fm["notion_parent"], "abc-123")
        self.assertEqual(body, "body")

    def test_double_quoted_value_stripped(self):
        fm, _ = ns.parse_front_matter('---\nnotion_parent: "abc-123"\n---\nb')
        self.assertEqual(fm["notion_parent"], "abc-123")

    def test_single_quoted_value_stripped(self):
        fm, _ = ns.parse_front_matter("---\nnotion_parent: 'abc-123'\n---\nb")
        self.assertEqual(fm["notion_parent"], "abc-123")

    def test_asymmetric_quotes_untouched(self):
        # Known limitation: only symmetric surrounding quotes are stripped.
        fm, _ = ns.parse_front_matter('---\ntitle: "a\'\n---\nb')
        self.assertEqual(fm["title"], '"a\'')


class TestInlineLinks(TreeCacheFixture):
    def test_plain_relative_link_unchanged(self):
        segs = ns.parse_inline_markdown("see [doc](T101.md) end")
        links = [s for s in segs if s.get("text", {}).get("link")]
        self.assertEqual(links[0]["text"]["link"]["url"], "T101.md")

    def test_http_link_unchanged(self):
        segs = ns.parse_inline_markdown("see [site](https://example.com)")
        links = [s for s in segs if s.get("text", {}).get("link")]
        self.assertEqual(links[0]["text"]["link"]["url"], "https://example.com")

    def test_anchor_link_textized(self):
        segs = _quiet(ns.parse_inline_markdown, "go [sec](#head) end")
        self.assertFalse(any(s.get("text", {}).get("link") for s in segs))
        self.assertTrue(any(s["text"]["content"] == "sec" for s in segs))

    def test_angle_unresolvable_textized_with_target(self):
        segs = _quiet(ns.parse_inline_markdown, "see [x](<No Such (Doc).md>)")
        self.assertFalse(any(s.get("text", {}).get("link") for s in segs))
        self.assertTrue(any(s["text"]["content"] == "x (No Such (Doc).md)" for s in segs))

    def test_angle_resolvable_becomes_notion_url(self):
        segs = _quiet(ns.parse_inline_markdown, "see [y](<ExistingPage.md>)")
        links = [s for s in segs if s.get("text", {}).get("link")]
        self.assertEqual(links[0]["text"]["link"]["url"],
                         "https://www.notion.so/" + "a" * 32)

    def test_angle_ambiguous_title_textized(self):
        segs = _quiet(ns.parse_inline_markdown, "see [z](<DupTitle.md>)")
        self.assertFalse(any(s.get("text", {}).get("link") for s in segs))

    def test_angle_ambiguous_resolved_by_parent_dir(self):
        segs = _quiet(ns.parse_inline_markdown, "see [z](<FolderA/DupTitle.md>)")
        links = [s for s in segs if s.get("text", {}).get("link")]
        self.assertEqual(links[0]["text"]["link"]["url"],
                         "https://www.notion.so/" + "b" * 32)


class TestInlineDecorations(unittest.TestCase):
    def test_basic_decorations(self):
        segs = ns.parse_inline_markdown("**b** *i* ~~s~~ `c` ***bi***")
        anns = [s.get("annotations", {}) for s in segs]
        self.assertTrue(any(a.get("bold") and not a.get("italic") for a in anns))
        self.assertTrue(any(a.get("italic") and not a.get("bold") for a in anns))
        self.assertTrue(any(a.get("strikethrough") for a in anns))
        self.assertTrue(any(a.get("code") for a in anns))
        self.assertTrue(any(a.get("bold") and a.get("italic") for a in anns))

    def test_span_color(self):
        segs = ns.parse_inline_markdown('<span color="red">赤</span> 通常')
        self.assertEqual(segs[0]["annotations"]["color"], "red")
        self.assertEqual(segs[0]["text"]["content"], "赤")

    def test_span_bg_shorthand_expanded(self):
        segs = ns.parse_inline_markdown('<span color="yellow_bg">黄</span>')
        self.assertEqual(segs[0]["annotations"]["color"], "yellow_background")

    def test_span_inner_bold_kept(self):
        segs = ns.parse_inline_markdown('<span color="red">a **b**</span>')
        bolds = [s for s in segs if s.get("annotations", {}).get("bold")]
        self.assertEqual(bolds[0]["annotations"]["color"], "red")

    def test_span_invalid_color_dropped(self):
        segs = _quiet(ns.parse_inline_markdown, '<span color="foo">text</span>')
        self.assertFalse(any(s.get("annotations", {}).get("color") for s in segs))
        self.assertTrue(any(s["text"]["content"] == "text" for s in segs))


class TestCalloutBlocks(unittest.TestCase):
    def test_multiline_callout(self):
        b = ns.markdown_to_notion_blocks(
            '<callout icon="🏷️" color="orange_bg">\n\t本文 **太字**\n</callout>')
        self.assertEqual(b[0]["type"], "callout")
        self.assertEqual(b[0]["callout"]["icon"], {"type": "emoji", "emoji": "🏷️"})
        self.assertEqual(b[0]["callout"]["color"], "orange_background")

    def test_one_line_callout(self):
        b = ns.markdown_to_notion_blocks('<callout icon="💡">ワンライナー</callout>')
        self.assertEqual(b[0]["type"], "callout")

    def test_unclosed_callout_not_swallowed(self):
        b = _quiet(ns.markdown_to_notion_blocks,
                   '<callout icon="💡">\n\t内容\n\n## 見出し\n残り')
        types = [x["type"] for x in b]
        self.assertEqual(types[0], "paragraph")
        self.assertIn("heading_2", types)

    def test_nested_callout_terminates_correctly(self):
        b = ns.markdown_to_notion_blocks(
            '<callout>\n\t外\n<callout>\n\t内\n</callout>\n</callout>\n\n段落')
        self.assertEqual(b[0]["type"], "callout")
        self.assertEqual(b[-1]["type"], "paragraph")

    def test_invalid_attrs_dropped(self):
        b = _quiet(ns.markdown_to_notion_blocks,
                   '<callout icon="star" color="foo">\n\t内容\n</callout>')
        self.assertEqual(b[0]["type"], "callout")
        self.assertNotIn("icon", b[0]["callout"])
        self.assertNotIn("color", b[0]["callout"])

    def test_legacy_quote_form_stays_quote(self):
        b = ns.markdown_to_notion_blocks("> 🏷️ 旧形式のcallout表現")
        self.assertEqual(b[0]["type"], "quote")


class TestDetailsBlocks(unittest.TestCase):
    def test_toggle_with_structured_children(self):
        b = ns.markdown_to_notion_blocks(
            "<details>\n<summary>親</summary>\n\t- リスト\n\t```py\n\tcode\n\t```\n"
            '\t<callout icon="💡">\n\t\tネスト\n\t</callout>\n</details>')
        self.assertEqual(b[0]["type"], "toggle")
        kid_types = [k["type"] for k in b[0]["toggle"]["children"]]
        self.assertEqual(kid_types, ["bulleted_list_item", "code", "callout"])

    def test_multiline_summary_via_br(self):
        b = ns.markdown_to_notion_blocks(
            "<details>\n<summary>行1<br>行2</summary>\n\t中身\n</details>")
        text = "".join(s["text"]["content"] for s in b[0]["toggle"]["rich_text"])
        self.assertEqual(text, "行1\n行2")

    def test_unclosed_details_not_swallowed(self):
        b = _quiet(ns.markdown_to_notion_blocks,
                   "<details>\n<summary>x</summary>\n残り\n## 見出し")
        types = [x["type"] for x in b]
        self.assertEqual(types[0], "paragraph")
        self.assertIn("heading_2", types)

    def test_children_capped_at_100(self):
        md = ("<details>\n<summary>x</summary>\n"
              + "\n".join("\tp%d" % i for i in range(105)) + "\n</details>")
        b = _quiet(ns.markdown_to_notion_blocks, md)
        self.assertEqual(len(b[0]["toggle"]["children"]), 100)

    def test_legacy_list_form_stays_list(self):
        b = ns.markdown_to_notion_blocks("- 旧形式トグルはリストのまま")
        self.assertEqual(b[0]["type"], "bulleted_list_item")


class TestEmitters(unittest.TestCase):
    def _callout_block(self):
        return {"type": "callout", "has_children": False, "callout": {
            "rich_text": [
                {"plain_text": "text ", "annotations": {}},
                {"plain_text": "bold", "annotations": {"bold": True}}],
            "icon": {"type": "emoji", "emoji": "🎯"},
            "color": "blue_background"}}

    def test_callout_emit_and_reparse(self):
        md = ns._block_to_md(self._callout_block())[0][1]
        self.assertTrue(md.startswith('<callout icon="🎯" color="blue_bg">'))
        b = ns.markdown_to_notion_blocks(md)
        self.assertEqual(b[0]["callout"]["color"], "blue_background")
        self.assertEqual(b[0]["callout"]["icon"]["emoji"], "🎯")

    def test_toggle_summary_newline_escaped(self):
        blk = {"type": "toggle", "has_children": False,
               "toggle": {"rich_text": [{"plain_text": "行1\n行2", "annotations": {}}]}}
        md = ns._block_to_md(blk)[0][1]
        self.assertIn("<summary>行1<br>行2</summary>", md)

    def test_span_emit_per_line(self):
        rt = [{"plain_text": "a\nb", "annotations": {"color": "red"}}]
        md = ns.rich_text_to_markdown(rt)
        self.assertEqual(md, '<span color="red">a</span>\n<span color="red">b</span>')

    def test_span_emit_and_reparse_with_bold(self):
        rt = [{"plain_text": "黄背景太字", "annotations":
               {"color": "yellow_background", "bold": True}}]
        md = ns.rich_text_to_markdown(rt)
        segs = ns.parse_inline_markdown(md)
        self.assertEqual(segs[0]["annotations"]["color"], "yellow_background")
        self.assertTrue(segs[0]["annotations"]["bold"])

    def test_emit_parse_textually_stable(self):
        # Two emit->parse->emit cycles must not drift (whitespace etc.)
        md1 = ns._block_to_md(self._callout_block())[0][1]
        b = ns.markdown_to_notion_blocks(md1)
        rt = b[0]["callout"]["rich_text"]
        for seg in rt:  # parse produces text.content; emitter reads plain_text
            seg["plain_text"] = seg.get("text", {}).get("content", "")
        blk2 = {"type": "callout", "has_children": False,
                "callout": {"rich_text": rt, "icon": b[0]["callout"]["icon"],
                            "color": b[0]["callout"]["color"]}}
        md2 = ns._block_to_md(blk2)[0][1]
        self.assertEqual(md1, md2)


class TestUndetectedChildLinks(unittest.TestCase):
    def test_detects_plain_md_link(self):
        r = ns._find_undetected_md_links("[childB](childB/childB.md)")
        self.assertEqual(r, [("childB", "childB/childB.md")])

    def test_proper_child_link_excluded(self):
        r = ns._find_undetected_md_links("[📄 childA](<childA/childA.md>)")
        self.assertEqual(r, [])

    def test_bulleted_icon_link_detected(self):
        r = ns._find_undetected_md_links("- [📄 bullet](bullet/bullet.md)")
        self.assertEqual(len(r), 1)

    def test_code_fence_skipped(self):
        r = ns._find_undetected_md_links("```\n[in_fence](x.md)\n```\n[real](real.md)")
        self.assertEqual([t for _, t in r], ["real.md"])

    def test_external_and_media_links_excluded(self):
        r = ns._find_undetected_md_links(
            "[ext](https://a.com/b.md)\n[📎 file](_assets/f.pdf)")
        self.assertEqual(r, [])


class WorkspaceFixture(unittest.TestCase):
    """Temp workspace with tree_cache + sync_state and CFG paths pointed at it."""

    ROOT_ID = "f" * 32
    PARENT_ID = "a" * 32
    TREE = [
        {"type": "page", "title": "Root", "path": "Root",
         "id": ROOT_ID, "depth": -1, "has_children": True, "is_root": True},
        {"type": "page", "title": "Parent", "path": "Parent",
         "id": PARENT_ID, "depth": 0, "has_children": True},
        {"type": "page", "title": "Sub", "path": "Parent/Sub",
         "id": "b" * 32, "depth": 1, "has_children": False},
    ]

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="nsync-test-ws-"))
        sync_dir = self.ws / "_nsync"
        sync_dir.mkdir()
        (sync_dir / "tree_cache.json").write_text(json.dumps(self.TREE))
        self._old = (ns.CFG.sync_dir, ns.CFG.base_output_dir,
                     ns.CFG.tree_json, ns.CFG.sync_state_json)
        ns.CFG.sync_dir = sync_dir
        ns.CFG.base_output_dir = self.ws
        ns.CFG.tree_json = sync_dir / "tree_cache.json"
        ns.CFG.sync_state_json = sync_dir / "sync_state.json"

    def tearDown(self):
        (ns.CFG.sync_dir, ns.CFG.base_output_dir,
         ns.CFG.tree_json, ns.CFG.sync_state_json) = self._old
        shutil.rmtree(self.ws, ignore_errors=True)


class TestScaffoldCanonicalPath(WorkspaceFixture):
    def test_new_under_parent_folder(self):
        ok = _quiet(ns.cmd_new, "Parent", "Child")
        self.assertTrue(ok)
        page = self.ws / "Parent" / "Child.md"
        self.assertTrue(page.exists())
        self.assertFalse((self.ws / "Child.md").exists())
        fm, _ = ns.parse_front_matter(page.read_text(encoding="utf-8"))
        self.assertEqual(fm.get("notion_parent"), self.PARENT_ID)

    def test_new_container_with_children_under_parent(self):
        ok = _quiet(ns.cmd_new, "Parent", "Plan", children=["MemoA"])
        self.assertTrue(ok)
        self.assertTrue((self.ws / "Parent" / "Plan" / "Plan.md").exists())
        # Children scaffold in flat form — the canonical path item_to_filepath
        # resolves a childless page to (container form would duplicate on sync)
        self.assertTrue((self.ws / "Parent" / "Plan" / "MemoA.md").exists())
        self.assertFalse((self.ws / "Parent" / "Plan" / "MemoA" / "MemoA.md").exists())
        # Parent body links to the flat path
        body = (self.ws / "Parent" / "Plan" / "Plan.md").read_text(encoding="utf-8")
        self.assertIn("(MemoA.md)", body)

    def test_new_root_parent_scaffolds_at_ws_root(self):
        ok = _quiet(ns.cmd_new, "Root", "TopPage")
        self.assertTrue(ok)
        self.assertTrue((self.ws / "TopPage.md").exists())

    def test_new_unknown_parent_falls_back_to_ws_root(self):
        url = "https://www.notion.so/x/Page-" + "9" * 32
        ok = _quiet(ns.cmd_new, url, "Orphan")
        self.assertTrue(ok)
        self.assertTrue((self.ws / "Orphan.md").exists())

    def test_new_local_file_parent_not_in_tree(self):
        doc = self.ws / "Some" / "Doc.md"
        doc.parent.mkdir(parents=True)
        doc.write_text("---\nnotion_id: %s\n---\n\nbody\n" % ("9" * 32),
                       encoding="utf-8")
        ok = _quiet(ns.cmd_new, str(doc), "Child")
        self.assertTrue(ok)
        self.assertTrue((self.ws / "Some" / "Doc" / "Child.md").exists())


class TestDuplicateNotionIdGuard(WorkspaceFixture):
    DASHED = "cccccccc-cccc-cccc-cccc-cccccccccccc"

    def _write(self, relpath, notion_id):
        fp = self.ws / relpath
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text("---\nnotion_id: %s\n---\n\nbody\n" % notion_id,
                      encoding="utf-8")
        return fp

    def test_detect_duplicate_notion_ids(self):
        self._write("Parent/Child.md", self.DASHED)
        self._write("Child.md", "c" * 32)  # same id, dashless form
        self._write("Parent/Sub.md", "b" * 32)  # unique id
        dupes = ns._detect_duplicate_notion_ids()
        self.assertEqual(list(dupes.keys()), ["c" * 32])
        self.assertEqual(len(dupes["c" * 32]), 2)

    def test_push_refuses_stale_duplicate(self):
        tracked = self._write("Parent/Child.md", self.DASHED)
        stale = self._write("Child.md", self.DASHED)
        ns.save_sync_state({"items": {"c" * 32: {
            "title": "Child", "path": "Parent/Child", "type": "page",
            "synced_at": "2026-07-10T00:00:00", "last_edited_time": ""}}})
        self.assertEqual(ns._canonical_tracked_filepath(self.DASHED), tracked)
        ok = _quiet(ns.cmd_push, str(stale))
        self.assertFalse(ok)
        # the tracked file itself is not blocked by the guard
        self.assertEqual(ns._canonical_tracked_filepath("c" * 32), tracked)


if __name__ == "__main__":
    unittest.main()
