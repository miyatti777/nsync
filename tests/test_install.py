# Tests for the `install` subcommand — stdlib only, no Notion API required.
#
# Run:  python3 -m unittest discover tests        (from the repo root)
#  or:  python3 -m pytest tests                   (if pytest is installed)
#
# Covers the canonical-placement / anti-footgun install mechanism (T011):
#   - target resolution (claude/cursor/codex/global/--dir, git-root awareness)
#   - copy into a fresh canonical dir
#   - idempotency: re-run on the canonical location is a no-op
#   - refuses to clobber a differing existing install without --force
#   - --force updates an existing install
#   - never overwrites an existing .env (token safety)
#   - refuses nested source/target

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "nsync.py"
_spec = importlib.util.spec_from_file_location("nsync", _SCRIPT)
ns = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ns)


def _make_source(root):
    """Create a minimal fake nsync source tree under `root`."""
    root = Path(root)
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "nsync.py").write_text("# fake\n", encoding="utf-8")
    (root / "scripts" / "__pycache__").mkdir(exist_ok=True)
    (root / "scripts" / "__pycache__" / "x.pyc").write_text("junk", encoding="utf-8")
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "guide.md").write_text("doc\n", encoding="utf-8")
    (root / "SKILL.md").write_text("skill\n", encoding="utf-8")
    (root / "README.md").write_text("readme\n", encoding="utf-8")
    (root / "LICENSE").write_text("mit\n", encoding="utf-8")
    # Secrets / runtime cruft that must NOT be copied:
    (root / ".env").write_text("NOTION_API_TOKEN=secret123\n", encoding="utf-8")
    (root / "_sync").mkdir(exist_ok=True)
    (root / "_sync" / "state.json").write_text("{}", encoding="utf-8")
    return root


class ResolveTargetTests(unittest.TestCase):
    def test_global(self):
        t = ns._resolve_install_target("global", None, "/some/cwd", "/home/me")
        self.assertEqual(t, Path("/home/me/.claude/skills/nsync").resolve())

    def test_dir_override(self):
        with tempfile.TemporaryDirectory() as d:
            t = ns._resolve_install_target("claude", d, "/ignored", "/home/me")
            self.assertEqual(t, (Path(d).resolve() / ".claude/skills/nsync").resolve())

    def test_codex_dir_override(self):
        with tempfile.TemporaryDirectory() as d:
            t = ns._resolve_install_target("codex", d, "/ignored", "/home/me")
            self.assertEqual(t, (Path(d).resolve() / ".agents/skills/nsync").resolve())

    def test_claude_uses_git_root(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            (root / ".git").mkdir()
            sub = root / "a" / "b"
            sub.mkdir(parents=True)
            t = ns._resolve_install_target("claude", None, str(sub), "/home/me")
            self.assertEqual(t, (root / ".claude/skills/nsync").resolve())

    def test_claude_no_git_falls_back_to_cwd(self):
        with tempfile.TemporaryDirectory() as d:
            cwd = Path(d).resolve()
            t = ns._resolve_install_target("claude", None, str(cwd), "/home/me")
            self.assertEqual(t, (cwd / ".claude/skills/nsync").resolve())

    def test_codex_uses_git_root(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            (root / ".git").mkdir()
            sub = root / "a" / "b"
            sub.mkdir(parents=True)
            t = ns._resolve_install_target("codex", None, str(sub), "/home/me")
            self.assertEqual(t, (root / ".agents/skills/nsync").resolve())


class InstallSkillTests(unittest.TestCase):
    def _run(self, *a, **k):
        with contextlib.redirect_stdout(io.StringIO()):
            return ns.install_skill(*a, **k)

    def test_fresh_install_copies_allowlist_only(self):
        with tempfile.TemporaryDirectory() as d:
            src = _make_source(Path(d) / "src")
            target = Path(d) / "dest" / ".claude/skills/nsync"
            res = self._run(src, target)
            self.assertEqual(res["status"], "installed")
            # Allowlisted content present
            self.assertTrue((target / "scripts" / "nsync.py").exists())
            self.assertTrue((target / "docs" / "guide.md").exists())
            self.assertTrue((target / "SKILL.md").exists())
            # Cruft / secrets NOT copied
            self.assertFalse((target / ".env").exists() and
                             (target / ".env").read_text() == "NOTION_API_TOKEN=secret123\n")
            self.assertFalse((target / "_sync").exists())
            self.assertFalse((target / "scripts" / "__pycache__").exists())
            # A blank .env template was scaffolded
            self.assertTrue((target / ".env").exists())
            self.assertIn("NOTION_API_TOKEN=", (target / ".env").read_text())
            self.assertTrue(res["env_created"])

    def test_fresh_codex_install_uses_agents_skill_dir(self):
        with tempfile.TemporaryDirectory() as d:
            src = _make_source(Path(d) / "src")
            project = Path(d) / "project"
            target = ns._resolve_install_target("codex", str(project), "/ignored", "/home/me")
            res = self._run(src, target)
            self.assertEqual(res["status"], "installed")
            self.assertEqual(target, (project / ".agents/skills/nsync").resolve())
            self.assertTrue((target / "SKILL.md").exists())
            self.assertTrue((target / "scripts" / "nsync.py").exists())

    def test_already_canonical_is_noop_copy(self):
        with tempfile.TemporaryDirectory() as d:
            src = _make_source(Path(d) / "nsync")
            res = self._run(src, src)
            self.assertEqual(res["status"], "already_canonical")
            self.assertEqual(res["copied"], [])
            # Existing .env with a real token is preserved untouched
            self.assertEqual((src / ".env").read_text(), "NOTION_API_TOKEN=secret123\n")
            self.assertFalse(res["env_created"])

    def test_refuses_existing_without_force(self):
        with tempfile.TemporaryDirectory() as d:
            src = _make_source(Path(d) / "src")
            target = _make_source(Path(d) / "dest" / ".claude/skills/nsync")
            res = self._run(src, target)
            self.assertEqual(res["status"], "exists")
            self.assertEqual(res["copied"], [])

    def test_force_updates_existing(self):
        with tempfile.TemporaryDirectory() as d:
            src = _make_source(Path(d) / "src")
            (src / "SKILL.md").write_text("NEW CONTENT\n", encoding="utf-8")
            target = _make_source(Path(d) / "dest" / ".claude/skills/nsync")
            res = self._run(src, target, force=True)
            self.assertEqual(res["status"], "updated")
            self.assertEqual((target / "SKILL.md").read_text(), "NEW CONTENT\n")

    def test_never_overwrites_existing_env_on_force(self):
        with tempfile.TemporaryDirectory() as d:
            src = _make_source(Path(d) / "src")
            target = _make_source(Path(d) / "dest" / ".claude/skills/nsync")
            (target / ".env").write_text("NOTION_API_TOKEN=KEEPME\n", encoding="utf-8")
            self._run(src, target, force=True)
            self.assertEqual((target / ".env").read_text(), "NOTION_API_TOKEN=KEEPME\n")

    def test_refuses_nested_target_in_source(self):
        with tempfile.TemporaryDirectory() as d:
            src = _make_source(Path(d) / "src")
            nested = src / ".claude/skills/nsync"
            res = self._run(src, nested)
            self.assertEqual(res["status"], "error")

    def test_refuses_nonempty_target_without_entrypoint(self):
        # Regression: a target holding user files but no scripts/nsync.py must NOT
        # be clobbered without --force (previously bypassed the guard and rmtree'd it).
        with tempfile.TemporaryDirectory() as d:
            src = _make_source(Path(d) / "src")
            target = Path(d) / "dest" / ".claude/skills/nsync"
            (target / "docs").mkdir(parents=True)
            precious = target / "docs" / "USER_PRECIOUS.md"
            precious.write_text("do not delete\n", encoding="utf-8")
            res = self._run(src, target)
            self.assertEqual(res["status"], "exists")
            self.assertTrue(precious.exists())
            self.assertEqual(precious.read_text(), "do not delete\n")

    def test_refuses_target_path_that_is_a_file(self):
        with tempfile.TemporaryDirectory() as d:
            src = _make_source(Path(d) / "src")
            target = Path(d) / "dest" / ".claude/skills/nsync"
            target.parent.mkdir(parents=True)
            target.write_text("i am a file\n", encoding="utf-8")  # not a directory
            res = self._run(src, target)
            self.assertEqual(res["status"], "error")
            self.assertEqual(target.read_text(), "i am a file\n")

    def test_force_replaces_symlinked_subdir(self):
        # --force update must not crash when an allowlist dir is a symlink.
        with tempfile.TemporaryDirectory() as d:
            src = _make_source(Path(d) / "src")
            target = _make_source(Path(d) / "dest" / ".claude/skills/nsync")
            real_docs = Path(d) / "elsewhere_docs"
            real_docs.mkdir()
            (real_docs / "old.md").write_text("old\n", encoding="utf-8")
            import shutil as _sh
            _sh.rmtree(target / "docs")  # replace real docs/ with a symlink
            (target / "docs").symlink_to(real_docs, target_is_directory=True)
            res = self._run(src, target, force=True)
            self.assertEqual(res["status"], "updated")
            self.assertFalse((target / "docs").is_symlink())
            self.assertTrue((target / "docs" / "guide.md").exists())


if __name__ == "__main__":
    unittest.main()
