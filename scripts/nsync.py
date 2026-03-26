#!/usr/bin/env python3
"""
nsync — Generic Notion Sync Tool
任意のNotionページ配下をローカルにミラーリングする。
- ページ → Markdown (.md)
- データベース → SQLite (個別 .db)
- 差分同期（last_edited_time 比較）
- push: ローカルMD → Notion更新（個別ページ）
"""

__version__ = "0.1.0"

import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


# ==========================================
# Config
# ==========================================

class Config:
    def __init__(self):
        self.root_page_id = ""
        self.label = ""
        self.base_output_dir = Path(".")
        self.sync_dir = Path(".")
        self.tree_json = Path(".")
        self.sync_state_json = Path(".")
        self.crawl_max_depth = 10
        self.rate_limit_delay = 0.35
        self.max_retries = 3
        self.retry_backoff = 2.0
        self.exclude_paths = ["_sync", "_archived"]
        self.db_page_content = False

CFG = Config()

TOKEN = ""
API_VERSION = ""
HEADERS = {}


def init_api():
    global TOKEN, API_VERSION, HEADERS
    TOKEN = os.environ.get("NOTION_API_TOKEN", "")
    API_VERSION = os.environ.get("NOTION_API_VERSION", "2022-06-28")
    HEADERS = {
        "Authorization": "Bearer " + TOKEN,
        "Notion-Version": API_VERSION,
        "Content-Type": "application/json",
    }


def load_config(config_path):
    p = Path(config_path).resolve()
    if not p.exists():
        print("ERROR: Config not found: %s" % p, flush=True)
        sys.exit(1)

    raw = p.read_text(encoding="utf-8")
    if HAS_YAML:
        data = yaml.safe_load(raw)
    else:
        data = _parse_simple_yaml(raw)

    CFG.root_page_id = data.get("root_page_id", "")
    CFG.label = data.get("label", "Notion")
    CFG.base_output_dir = p.parent
    CFG.sync_dir = p.parent / "_sync"
    CFG.tree_json = CFG.sync_dir / "tree_cache.json"
    CFG.sync_state_json = CFG.sync_dir / "sync_state.json"
    CFG.crawl_max_depth = data.get("crawl_max_depth", 10)
    CFG.rate_limit_delay = data.get("rate_limit_delay", 0.35)
    CFG.max_retries = data.get("max_retries", 3)
    CFG.retry_backoff = data.get("retry_backoff", 2.0)
    CFG.exclude_paths = data.get("exclude_paths", ["_sync", "_archived"])
    CFG.db_page_content = data.get("db_page_content", False)


def _parse_simple_yaml(text):
    """PyYAML が無い環境用の簡易パーサー"""
    data = {}
    current_list_key = None
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            current_list_key = None
            continue
        if stripped.startswith("- ") and current_list_key:
            data.setdefault(current_list_key, []).append(stripped[2:].strip().strip('"').strip("'"))
            continue
        current_list_key = None
        if ":" in stripped:
            key, val = stripped.split(":", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if not val:
                current_list_key = key
                continue
            if val.lower() == "true":
                data[key] = True
            elif val.lower() == "false":
                data[key] = False
            else:
                try:
                    if "." in val:
                        data[key] = float(val)
                    else:
                        data[key] = int(val)
                except ValueError:
                    data[key] = val
    return data


# ==========================================
# Utilities
# ==========================================

def sanitize_filename(name):
    name = re.sub(r'[<>:"/\\|?*/]', '_', name)
    name = re.sub(r'\s+', ' ', name).strip()
    if len(name) > 200:
        name = name[:200]
    return name


def sanitize_table_name(name):
    name = re.sub(r'[^a-zA-Z0-9_\u3000-\u9fff\u4e00-\u9faf]', '_', name)
    name = re.sub(r'_+', '_', name).strip('_')
    if not name or name[0].isdigit():
        name = 'db_' + name
    return name[:60]


def extract_page_id_from_url(url):
    """Notion URL からページ ID を抽出 (ハイフン付き32桁UUID)"""
    match = re.search(r'([0-9a-f]{32})', url)
    if match:
        raw = match.group(1)
        return "%s-%s-%s-%s-%s" % (raw[:8], raw[8:12], raw[12:16], raw[16:20], raw[20:])
    match = re.search(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', url)
    if match:
        return match.group(1)
    return ""


# ==========================================
# Notion API
# ==========================================

def api_get(url, retries=None):
    if retries is None:
        retries = CFG.max_retries
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = CFG.retry_backoff ** (attempt + 1)
                print("  Rate limited. Waiting %ss..." % wait, flush=True)
                time.sleep(wait)
                continue
            elif e.code == 404:
                return None
            else:
                print("  HTTP %d on attempt %d: %s" % (e.code, attempt + 1, url), flush=True)
                if attempt < retries - 1:
                    time.sleep(CFG.retry_backoff ** attempt)
                    continue
                return None
        except Exception as e:
            print("  Error on attempt %d: %s" % (attempt + 1, e), flush=True)
            if attempt < retries - 1:
                time.sleep(CFG.retry_backoff ** attempt)
                continue
            return None
    return None


def api_post(url, body_dict, retries=None):
    if retries is None:
        retries = CFG.max_retries
    body = json.dumps(body_dict).encode()
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, headers=HEADERS, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = CFG.retry_backoff ** (attempt + 1)
                time.sleep(wait)
                continue
            elif e.code == 404:
                return None
            else:
                if attempt < retries - 1:
                    time.sleep(CFG.retry_backoff ** attempt)
                    continue
                return None
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(CFG.retry_backoff ** attempt)
                continue
            return None
    return None


def api_patch(url, body_dict, retries=None):
    if retries is None:
        retries = CFG.max_retries
    body = json.dumps(body_dict).encode()
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, headers=HEADERS, method="PATCH")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = CFG.retry_backoff ** (attempt + 1)
                time.sleep(wait)
                continue
            else:
                print("  HTTP %d on attempt %d: %s" % (e.code, attempt + 1, url), flush=True)
                if attempt < retries - 1:
                    time.sleep(CFG.retry_backoff ** attempt)
                    continue
                return None
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(CFG.retry_backoff ** attempt)
                continue
            return None
    return None


def api_delete(url, retries=None):
    if retries is None:
        retries = CFG.max_retries
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=HEADERS, method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = CFG.retry_backoff ** (attempt + 1)
                time.sleep(wait)
                continue
            else:
                if attempt < retries - 1:
                    time.sleep(CFG.retry_backoff ** attempt)
                    continue
                return None
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(CFG.retry_backoff ** attempt)
                continue
            return None
    return None


# ==========================================
# Tree Crawl
# ==========================================

def get_blocks(block_id):
    results = []
    cursor = None
    while True:
        url = "https://api.notion.com/v1/blocks/%s/children?page_size=100" % block_id
        if cursor:
            url += "&start_cursor=" + cursor
        data = api_get(url)
        if not data:
            return results
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        time.sleep(CFG.rate_limit_delay)
    return results


CHECKPOINT_INTERVAL = 50

def _checkpoint_path():
    return CFG.sync_dir / "crawl_checkpoint.json"


def _save_checkpoint(items, queue):
    CFG.sync_dir.mkdir(parents=True, exist_ok=True)
    cp = {
        "items": items,
        "queue": queue,
        "saved_at": datetime.now().isoformat(),
    }
    with open(_checkpoint_path(), "w") as f:
        json.dump(cp, f, ensure_ascii=False)


def _load_checkpoint():
    p = _checkpoint_path()
    if not p.exists():
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return None


def _remove_checkpoint():
    p = _checkpoint_path()
    if p.exists():
        p.unlink()


def deep_crawl(block_id, path="", depth=0, resume_items=None, resume_queue=None):
    """BFS crawl with periodic checkpoint saves for resumability."""
    if resume_items is not None and resume_queue is not None:
        items = resume_items
        queue = [q for q in resume_queue if q[2] <= CFG.crawl_max_depth]
        skipped = len(resume_queue) - len(queue)
        visited = set(i["id"] for i in items)
        print("  Resuming from checkpoint: %d items, %d queue" % (len(items), len(queue)), flush=True)
        if skipped:
            print("  Skipped %d queue entries exceeding max_depth=%d" % (skipped, CFG.crawl_max_depth), flush=True)
    else:
        items = []
        queue = [(block_id, path, depth)]
        visited = set()

    count = len(items)
    last_checkpoint = count

    while queue:
        current_id, current_path, current_depth = queue.pop(0)

        blocks = get_blocks(current_id)
        for b in blocks:
            t = b["type"]
            bid = b["id"]
            hc = b.get("has_children", False)

            if t == "child_page":
                title = b["child_page"]["title"]
                full_path = current_path + "/" + title if current_path else title
                if bid not in visited:
                    visited.add(bid)
                    count += 1
                    if count % 10 == 0:
                        print("  ... found %d items (depth=%d)" % (count, current_depth), flush=True)
                    items.append({
                        "type": "page", "title": title, "path": full_path,
                        "id": bid, "depth": current_depth, "has_children": hc
                    })
                    if hc and current_depth < CFG.crawl_max_depth:
                        queue.append((bid, full_path, current_depth + 1))
            elif t == "child_database":
                title = b["child_database"]["title"]
                full_path = current_path + "/" + title if current_path else title
                if bid not in visited:
                    visited.add(bid)
                    count += 1
                    items.append({
                        "type": "db", "title": title, "path": full_path,
                        "id": bid, "depth": current_depth
                    })
            elif hc and current_depth < CFG.crawl_max_depth:
                if bid not in visited:
                    visited.add(bid)
                    queue.append((bid, current_path, current_depth))

            if count - last_checkpoint >= CHECKPOINT_INTERVAL:
                _save_checkpoint(items, queue)
                last_checkpoint = count
                print("  [checkpoint saved: %d items, %d remaining in queue]" % (count, len(queue)), flush=True)

        time.sleep(CFG.rate_limit_delay)

    _remove_checkpoint()
    return items


# ==========================================
# Rich Text Helpers
# ==========================================

def rich_text_to_markdown(rt_list):
    """Convert Notion rich_text array to Markdown string with formatting."""
    parts = []
    for seg in rt_list:
        text = seg.get("plain_text", "")
        if not text:
            continue
        ann = seg.get("annotations", {})
        href = seg.get("href")

        if ann.get("code"):
            text = "`%s`" % text
        if ann.get("bold"):
            text = "**%s**" % text
        if ann.get("italic"):
            text = "*%s*" % text
        if ann.get("strikethrough"):
            text = "~~%s~~" % text
        if href:
            text = "[%s](%s)" % (text, href)

        parts.append(text)
    return "".join(parts)


# ==========================================
# Content Fetchers — Pages
# ==========================================

def _get_child_blocks(block_id):
    """Fetch children of a block (for nested content)."""
    results = []
    cursor = None
    while True:
        url = "https://api.notion.com/v1/blocks/%s/children?page_size=100" % block_id
        if cursor:
            url += "&start_cursor=" + cursor
        data = api_get(url)
        if not data:
            break
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        time.sleep(CFG.rate_limit_delay)
    return results


def _block_to_md(b, depth=0):
    """Convert a single Notion block to (btype, md_line) tuples, handling nesting."""
    btype = b["type"]
    if btype in ("child_page", "child_database"):
        return []
    block_data = b.get(btype, {})
    rt = block_data.get("rich_text", [])
    text = rich_text_to_markdown(rt)
    plain = "".join(t.get("plain_text", "") for t in rt)
    indent = "  " * depth

    NESTABLE = ("bulleted_list_item", "numbered_list_item", "to_do", "toggle", "quote", "callout")
    md_line = ""

    if btype == "heading_1":
        md_line = "# " + text
    elif btype == "heading_2":
        md_line = "## " + text
    elif btype == "heading_3":
        md_line = "### " + text
    elif btype == "bulleted_list_item":
        md_line = indent + "- " + text
    elif btype == "numbered_list_item":
        md_line = indent + "1. " + text
    elif btype == "to_do":
        checked = block_data.get("checked", False)
        mark = "x" if checked else " "
        md_line = indent + "- [%s] %s" % (mark, text)
    elif btype == "toggle":
        md_line = indent + "- " + text
    elif btype == "code":
        lang = block_data.get("language", "")
        md_line = "```%s\n%s\n```" % (lang, plain)
    elif btype == "quote":
        md_line = "> " + text
    elif btype == "callout":
        icon = b.get(btype, {}).get("icon", {})
        emoji = icon.get("emoji", "") if icon.get("type") == "emoji" else ""
        md_line = "> %s %s" % (emoji, text)
    elif btype == "divider":
        md_line = "---"
    elif btype == "image":
        img = block_data
        caption_rt = img.get("caption", [])
        caption = rich_text_to_markdown(caption_rt) if caption_rt else ""
        img_url = ""
        if img.get("type") == "file":
            img_url = img.get("file", {}).get("url", "")
        elif img.get("type") == "external":
            img_url = img.get("external", {}).get("url", "")
        if img_url:
            md_line = "![%s](%s)" % (caption, img_url)
    elif btype == "bookmark":
        bm_url = block_data.get("url", "")
        caption_rt = block_data.get("caption", [])
        caption = rich_text_to_markdown(caption_rt) if caption_rt else bm_url
        if bm_url:
            md_line = "[%s](%s)" % (caption, bm_url)
    elif btype == "embed":
        embed_url = block_data.get("url", "")
        if embed_url:
            md_line = "[embed](%s)" % embed_url
    elif btype == "equation":
        expr = block_data.get("expression", "")
        if expr:
            md_line = "$$%s$$" % expr
    elif btype == "table":
        table_lines = fetch_table_as_markdown(b["id"])
        md_line = "\n".join(table_lines)
    elif text:
        md_line = indent + text if depth > 0 else text

    entries = []
    if md_line:
        entries.append((btype, md_line))

    if b.get("has_children") and btype in NESTABLE and depth < 3:
        children = _get_child_blocks(b["id"])
        for child in children:
            entries.extend(_block_to_md(child, depth + 1))

    return entries


def fetch_page_blocks_as_text(page_id):
    blocks = []
    cursor = None
    while True:
        url = "https://api.notion.com/v1/blocks/%s/children?page_size=100" % page_id
        if cursor:
            url += "&start_cursor=" + cursor
        data = api_get(url)
        if not data:
            break
        blocks.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        time.sleep(CFG.rate_limit_delay)

    LIST_TYPES = ("bulleted_list_item", "numbered_list_item", "to_do", "toggle")
    entries = []
    for b in blocks:
        entries.extend(_block_to_md(b, depth=0))

    result_parts = []
    for i, (btype, md_line) in enumerate(entries):
        if i == 0:
            result_parts.append(md_line)
            continue
        prev_type = entries[i - 1][0]
        both_list = prev_type in LIST_TYPES and btype in LIST_TYPES
        if both_list:
            result_parts.append("\n" + md_line)
        else:
            result_parts.append("\n\n" + md_line)

    return "".join(result_parts)


def fetch_table_as_markdown(block_id):
    rows = get_blocks(block_id)
    if not rows:
        return []
    md_lines = []
    for i, row in enumerate(rows):
        cells = row.get("table_row", {}).get("cells", [])
        cell_texts = ["".join(t.get("plain_text", "") for t in cell) for cell in cells]
        md_lines.append("| " + " | ".join(cell_texts) + " |")
        if i == 0:
            md_lines.append("| " + " | ".join(["---"] * len(cell_texts)) + " |")
    return md_lines


# ==========================================
# Content Fetchers — Databases → SQLite
# ==========================================

def extract_property_value(prop):
    ptype = prop.get("type", "")
    if ptype == "title":
        return "".join(t.get("plain_text", "") for t in prop.get("title", []))
    elif ptype == "rich_text":
        return "".join(t.get("plain_text", "") for t in prop.get("rich_text", []))
    elif ptype == "number":
        val = prop.get("number")
        return str(val) if val is not None else ""
    elif ptype == "select":
        sel = prop.get("select")
        return sel.get("name", "") if sel else ""
    elif ptype == "multi_select":
        return ", ".join(s.get("name", "") for s in prop.get("multi_select", []))
    elif ptype == "date":
        d = prop.get("date")
        return d.get("start", "") if d else ""
    elif ptype == "checkbox":
        return str(prop.get("checkbox", False))
    elif ptype == "url":
        return prop.get("url", "") or ""
    elif ptype == "status":
        s = prop.get("status")
        return s.get("name", "") if s else ""
    elif ptype == "relation":
        return ", ".join(r.get("id", "") for r in prop.get("relation", []))
    elif ptype == "people":
        return ", ".join(p.get("name", p.get("id", "")) for p in prop.get("people", []))
    elif ptype == "created_time":
        return prop.get("created_time", "")
    elif ptype == "last_edited_time":
        return prop.get("last_edited_time", "")
    elif ptype == "formula":
        f = prop.get("formula", {})
        ftype = f.get("type", "")
        return str(f.get(ftype, ""))
    elif ptype == "rollup":
        r = prop.get("rollup", {})
        rtype = r.get("type", "")
        if rtype == "array":
            return str(len(r.get("array", [])))
        return str(r.get(rtype, ""))
    elif ptype == "email":
        return prop.get("email", "") or ""
    elif ptype == "phone_number":
        return prop.get("phone_number", "") or ""
    elif ptype == "files":
        files = prop.get("files", [])
        urls = []
        for f in files:
            if f.get("type") == "file":
                urls.append(f.get("file", {}).get("url", ""))
            elif f.get("type") == "external":
                urls.append(f.get("external", {}).get("url", ""))
        return ", ".join(urls)
    else:
        return str(prop.get(ptype, ""))


def db_filepath(db_title, db_path=""):
    if db_path:
        parts = db_path.split("/")
        sanitized = [sanitize_filename(p) for p in parts]
        if len(sanitized) > 1:
            dir_path = CFG.base_output_dir / os.path.join(*sanitized[:-1])
        else:
            dir_path = CFG.base_output_dir
    else:
        dir_path = CFG.base_output_dir
    dir_path.mkdir(parents=True, exist_ok=True)
    return dir_path / (sanitize_table_name(db_title) + ".db")


def fetch_database_to_sqlite(db_id, db_title, db_path):
    all_rows = []
    all_props = set()
    cursor = None

    while True:
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        data = api_post("https://api.notion.com/v1/databases/%s/query" % db_id, payload)
        if not data:
            break

        for page in data.get("results", []):
            props = page.get("properties", {})
            row = {"_notion_page_id": page["id"]}
            for key, val in props.items():
                all_props.add(key)
                row[key] = extract_property_value(val)
            row["_created_time"] = page.get("created_time", "")
            row["_last_edited_time"] = page.get("last_edited_time", "")
            all_rows.append(row)

        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        time.sleep(CFG.rate_limit_delay)

    if not all_rows:
        return 0

    if CFG.db_page_content:
        total = len(all_rows)
        print("    Fetching page body for %d rows..." % total, flush=True)
        for idx, row in enumerate(all_rows):
            pid = row.get("_notion_page_id", "")
            if pid:
                try:
                    row["_body"] = fetch_page_blocks_as_text(pid)
                except Exception:
                    row["_body"] = ""
                time.sleep(CFG.rate_limit_delay)
            if (idx + 1) % 20 == 0:
                print("    ... body %d/%d" % (idx + 1, total), flush=True)

    columns = sorted(all_props)
    sqlite_path = db_filepath(db_title, db_path)

    conn = sqlite3.connect(str(sqlite_path))
    c = conn.cursor()

    c.execute("DROP TABLE IF EXISTS data")

    col_defs = ["_notion_page_id TEXT PRIMARY KEY"]
    for col in columns:
        col_defs.append("[%s] TEXT" % col)
    col_defs.append("_created_time TEXT")
    col_defs.append("_last_edited_time TEXT")
    if CFG.db_page_content:
        col_defs.append("_body TEXT")

    c.execute("CREATE TABLE data (%s)" % ", ".join(col_defs))

    now = datetime.now().isoformat()
    for row in all_rows:
        vals = [row.get("_notion_page_id", "")]
        for col in columns:
            vals.append(row.get(col, ""))
        vals.append(row.get("_created_time", ""))
        vals.append(row.get("_last_edited_time", ""))
        if CFG.db_page_content:
            vals.append(row.get("_body", ""))
        placeholders = ", ".join(["?"] * len(vals))
        c.execute("INSERT INTO data VALUES (%s)" % placeholders, vals)

    c.execute("""CREATE TABLE IF NOT EXISTS _metadata (
        key TEXT PRIMARY KEY, value TEXT
    )""")
    for k, v in [
        ("notion_db_id", db_id), ("notion_db_path", db_path),
        ("db_title", db_title), ("row_count", str(len(all_rows))),
        ("synced_at", now),
    ]:
        c.execute("INSERT OR REPLACE INTO _metadata VALUES (?, ?)", (k, v))

    conn.commit()
    conn.close()
    return len(all_rows)


# ==========================================
# Sync State
# ==========================================

def load_sync_state():
    if CFG.sync_state_json.exists():
        with open(CFG.sync_state_json) as f:
            return json.load(f)
    return {"items": {}, "last_full_crawl": None}


def save_sync_state(state):
    CFG.sync_dir.mkdir(parents=True, exist_ok=True)
    with open(CFG.sync_state_json, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def get_page_last_edited(page_id):
    url = "https://api.notion.com/v1/pages/%s" % page_id
    data = api_get(url)
    if data:
        return data.get("last_edited_time", "")
    return ""


def item_to_filepath(item):
    path_parts = item["path"].split("/")
    sanitized = [sanitize_filename(p) for p in path_parts]

    if item["type"] == "page":
        is_root = item.get("is_root", False)
        is_container = item.get("is_container", False)

        if is_root:
            dir_path = CFG.base_output_dir
            filename = sanitized[-1]
            if not filename.endswith(".md"):
                filename += ".md"
            return dir_path / filename
        elif is_container:
            dir_path = CFG.base_output_dir / os.path.join(*sanitized)
            filename = sanitized[-1]
            if not filename.endswith(".md"):
                filename += ".md"
            return dir_path / filename
        else:
            if len(sanitized) > 1:
                dir_path = CFG.base_output_dir / os.path.join(*sanitized[:-1])
            else:
                dir_path = CFG.base_output_dir
            filename = sanitized[-1]
            if not filename.endswith(".md"):
                filename += ".md"
            return dir_path / filename
    elif item["type"] == "db":
        return None
    return None


def download_item(item):
    if item["type"] == "page":
        filepath = item_to_filepath(item)
        if not filepath:
            return False
        filepath.parent.mkdir(parents=True, exist_ok=True)

        md = fetch_page_blocks_as_text(item["id"])
        now = datetime.now().isoformat()
        header = "---\nnotion_id: %s\nnotion_path: %s\nsynced_at: %s\n---\n\n" % (
            item["id"], item["path"], now
        )
        filepath.write_text(header + md, encoding="utf-8")
        return True

    elif item["type"] == "db":
        count = fetch_database_to_sqlite(item["id"], item["title"], item["path"])
        if count > 0:
            fpath = db_filepath(item["title"], item["path"])
            rel = fpath.relative_to(CFG.base_output_dir)
            print("    -> %s (%d rows)" % (rel, count), flush=True)
            return True
        return False

    return False


# ==========================================
# Push: Local MD → Notion
# ==========================================

def parse_front_matter(text):
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    fm = {}
    for line in parts[1].strip().split("\n"):
        if ":" in line:
            key, val = line.split(":", 1)
            fm[key.strip()] = val.strip()
    return fm, parts[2].strip()


_LANG_ALIASES = {
    "py": "python", "js": "javascript", "ts": "typescript",
    "rb": "ruby", "sh": "bash", "zsh": "bash", "yml": "yaml",
    "md": "markdown", "rs": "rust", "cs": "c#", "cpp": "c++",
    "objc": "objective-c", "kt": "kotlin", "tf": "hcl",
}


def _normalize_lang(lang):
    if not lang:
        return "plain text"
    low = lang.lower().strip()
    return _LANG_ALIASES.get(low, low)


def parse_inline_markdown(text):
    """Convert Markdown inline formatting to Notion rich_text segments."""
    segments = []
    _INLINE_RE = re.compile(
        r'(?P<bold_italic>\*\*\*(.+?)\*\*\*)'
        r'|(?P<bold>\*\*(.+?)\*\*)'
        r'|(?P<italic>\*(.+?)\*)'
        r'|(?P<strike>~~(.+?)~~)'
        r'|(?P<code>`([^`]+)`)'
        r'|(?P<link>\[([^\]]+)\]\(([^)]+)\))'
        r'|(?P<img>!\[([^\]]*)\]\(([^)]+)\))'
    )

    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            plain = text[pos:m.start()]
            if plain:
                segments.extend(_chunk_text(plain, {}))

        if m.group("bold_italic"):
            segments.extend(_chunk_text(m.group(2), {"bold": True, "italic": True}))
        elif m.group("bold"):
            segments.extend(_chunk_text(m.group(4), {"bold": True}))
        elif m.group("italic"):
            segments.extend(_chunk_text(m.group(6), {"italic": True}))
        elif m.group("strike"):
            segments.extend(_chunk_text(m.group(8), {"strikethrough": True}))
        elif m.group("code"):
            segments.extend(_chunk_text(m.group(10), {"code": True}))
        elif m.group("img"):
            pass
        elif m.group("link"):
            link_text = m.group(12)
            link_url = m.group(13)
            segments.append({
                "type": "text",
                "text": {"content": link_text, "link": {"url": link_url}}
            })
        pos = m.end()

    if pos < len(text):
        remaining = text[pos:]
        if remaining:
            segments.extend(_chunk_text(remaining, {}))

    return segments if segments else [{"type": "text", "text": {"content": text}}]


def _chunk_text(text, annotations, chunk_size=1800):
    """Split text into Notion-safe chunks with annotations."""
    chunks = []
    for i in range(0, max(len(text), 1), chunk_size):
        piece = text[i:i + chunk_size]
        seg = {"type": "text", "text": {"content": piece}}
        ann = {}
        for k in ("bold", "italic", "strikethrough", "code"):
            if annotations.get(k):
                ann[k] = True
        if ann:
            seg["annotations"] = ann
        chunks.append(seg)
    return chunks


def _img_re_match(line):
    m = re.match(r'^!\[([^\]]*)\]\(([^)]+)\)$', line.strip())
    if m:
        return m.group(1), m.group(2)
    return None, None


def markdown_to_notion_blocks(md_text):
    blocks = []
    lines = md_text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]

        if line.startswith("```"):
            lang = _normalize_lang(line[3:].strip())
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code_lines.append(lines[i])
                i += 1
            code_content = "\n".join(code_lines)
            rt = _chunk_text(code_content, {})
            blocks.append({
                "object": "block", "type": "code",
                "code": {"rich_text": rt, "language": lang}
            })
        elif line.startswith("### "):
            blocks.append({"object": "block", "type": "heading_3",
                "heading_3": {"rich_text": parse_inline_markdown(line[4:])}})
        elif line.startswith("## "):
            blocks.append({"object": "block", "type": "heading_2",
                "heading_2": {"rich_text": parse_inline_markdown(line[3:])}})
        elif line.startswith("# "):
            blocks.append({"object": "block", "type": "heading_1",
                "heading_1": {"rich_text": parse_inline_markdown(line[2:])}})
        elif line.startswith("- ["):
            checked = len(line) > 3 and line[3] == "x"
            text = line[6:] if len(line) > 6 else ""
            blocks.append({"object": "block", "type": "to_do",
                "to_do": {"rich_text": parse_inline_markdown(text), "checked": checked}})
        elif line.startswith("- "):
            blocks.append({"object": "block", "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": parse_inline_markdown(line[2:])}})
        elif re.match(r'^\d+\.\s', line):
            text = re.sub(r'^\d+\.\s', '', line)
            blocks.append({"object": "block", "type": "numbered_list_item",
                "numbered_list_item": {"rich_text": parse_inline_markdown(text)}})
        elif line.startswith("> "):
            blocks.append({"object": "block", "type": "quote",
                "quote": {"rich_text": parse_inline_markdown(line[2:])}})
        elif line.strip() == "---":
            blocks.append({"object": "block", "type": "divider", "divider": {}})
        else:
            alt, img_url = _img_re_match(line)
            if img_url:
                blocks.append({"object": "block", "type": "image",
                    "image": {"type": "external", "external": {"url": img_url},
                              "caption": parse_inline_markdown(alt) if alt else []}})
            elif line.strip():
                blocks.append({"object": "block", "type": "paragraph",
                    "paragraph": {"rich_text": parse_inline_markdown(line.strip())}})
        i += 1

    return blocks


def cmd_pull(filepath, dry_run=False):
    p = Path(filepath)
    if not p.exists():
        print("ERROR: File not found: %s" % filepath, flush=True)
        return False

    text = p.read_text(encoding="utf-8")
    fm, body = parse_front_matter(text)
    notion_id = fm.get("notion_id", "")
    notion_path = fm.get("notion_path", "")

    if not notion_id:
        print("ERROR: No notion_id in front matter of %s" % filepath, flush=True)
        return False

    if dry_run:
        print("=== Pull DRY RUN ===", flush=True)
        print("File: %s" % filepath, flush=True)
        print("Notion ID: %s" % notion_id, flush=True)
        print("Fetching blocks from Notion...", flush=True)
        print("", flush=True)

        blocks = []
        cursor = None
        while True:
            url = "https://api.notion.com/v1/blocks/%s/children?page_size=100" % notion_id
            if cursor:
                url += "&start_cursor=" + cursor
            data = api_get(url)
            if not data:
                break
            blocks.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            time.sleep(CFG.rate_limit_delay)

        print("Blocks on Notion: %d" % len(blocks), flush=True)
        print("", flush=True)
        for i, b in enumerate(blocks):
            bt = b["type"]
            if bt in ("child_page", "child_database"):
                title = b.get(bt, {}).get("title", "")
                print("  [%d] %s: %s" % (i + 1, bt, title), flush=True)
                continue
            rt = b.get(bt, {}).get("rich_text", [])
            preview = "".join(s.get("plain_text", "")[:60] for s in rt[:3])
            if not preview and bt == "divider":
                preview = "---"
            elif not preview and bt == "code":
                preview = "(code block)"
            elif not preview and bt == "image":
                img = b.get(bt, {})
                if img.get("type") == "file":
                    preview = img.get("file", {}).get("url", "")[:60]
                elif img.get("type") == "external":
                    preview = img.get("external", {}).get("url", "")[:60]
            print("  [%d] %s: %s" % (i + 1, bt, preview), flush=True)

        print("\n(dry-run: local file not modified)", flush=True)
        return True

    print("=== Pull from Notion ===", flush=True)
    print("File: %s" % filepath, flush=True)
    print("Notion ID: %s" % notion_id, flush=True)

    md = fetch_page_blocks_as_text(notion_id)
    now = datetime.now().isoformat()

    fm["synced_at"] = now
    if "pushed_at" in fm:
        del fm["pushed_at"]
    fm_lines = ["---"]
    for k, v in fm.items():
        fm_lines.append("%s: %s" % (k, v))
    fm_lines.append("---")
    p.write_text("\n".join(fm_lines) + "\n\n" + md, encoding="utf-8")

    lines = md.count("\n") + 1 if md else 0
    print("Downloaded: %d lines" % lines, flush=True)
    print("Pull complete.", flush=True)
    return True


def cmd_push(filepath, dry_run=False):
    p = Path(filepath)
    if not p.exists():
        print("ERROR: File not found: %s" % filepath, flush=True)
        return False

    text = p.read_text(encoding="utf-8")
    fm, body = parse_front_matter(text)
    notion_id = fm.get("notion_id", "")

    if not notion_id:
        print("ERROR: No notion_id in front matter of %s" % filepath, flush=True)
        return False

    new_blocks = markdown_to_notion_blocks(body)

    if dry_run:
        print("=== Push DRY RUN ===", flush=True)
        print("File: %s" % filepath, flush=True)
        print("Notion ID: %s" % notion_id, flush=True)
        print("Blocks to upload: %d" % len(new_blocks), flush=True)
        print("", flush=True)
        for i, blk in enumerate(new_blocks):
            bt = blk.get("type", "?")
            rt = blk.get(bt, {}).get("rich_text", [])
            preview = "".join(s.get("text", {}).get("content", "")[:60] for s in rt[:2])
            if not preview and bt == "divider":
                preview = "---"
            elif not preview and bt == "code":
                preview = "(code block)"
            elif not preview and bt == "image":
                preview = blk.get("image", {}).get("external", {}).get("url", "")[:60]
            print("  [%d] %s: %s" % (i + 1, bt, preview), flush=True)
        print("\n(dry-run: no changes written to Notion)", flush=True)
        return True

    print("=== Push to Notion ===", flush=True)
    print("File: %s" % filepath, flush=True)
    print("Notion ID: %s" % notion_id, flush=True)

    print("Removing existing blocks...", flush=True)
    existing = get_blocks(notion_id)
    for b in existing:
        if b["type"] in ("child_page", "child_database"):
            continue
        api_delete("https://api.notion.com/v1/blocks/%s" % b["id"])
        time.sleep(CFG.rate_limit_delay)

    print("Uploading %d blocks..." % len(new_blocks), flush=True)

    url = "https://api.notion.com/v1/blocks/%s/children" % notion_id
    for i in range(0, len(new_blocks), 100):
        chunk = new_blocks[i:i + 100]
        result = api_patch(url, {"children": chunk})
        if not result:
            print("  ERROR: Failed to append blocks (chunk %d)" % (i // 100), flush=True)
            return False
        time.sleep(CFG.rate_limit_delay)
        print("  Uploaded blocks %d-%d" % (i + 1, min(i + 100, len(new_blocks))), flush=True)

    now = datetime.now().isoformat()
    fm["synced_at"] = now
    fm["pushed_at"] = now
    fm_lines = ["---"]
    for k, v in fm.items():
        fm_lines.append("%s: %s" % (k, v))
    fm_lines.append("---")
    p.write_text("\n".join(fm_lines) + "\n\n" + body, encoding="utf-8")

    print("Push complete.", flush=True)
    return True


# ==========================================
# Main Commands
# ==========================================

def _mark_containers(items):
    """Mark pages that have child pages/dbs in the tree as containers."""
    paths = set(i["path"] for i in items)
    for item in items:
        if item["type"] != "page":
            continue
        p = item["path"]
        item["is_container"] = any(
            op.startswith(p + "/") for op in paths if op != p
        )


def cmd_crawl():
    print("=== Crawling %s (root: %s) ===" % (CFG.label, CFG.root_page_id), flush=True)
    print("Started: " + datetime.now().isoformat(), flush=True)

    root_title = CFG.label or "index"
    root_item = {
        "type": "page", "title": root_title,
        "path": root_title, "id": CFG.root_page_id,
        "depth": -1, "has_children": True, "is_root": True
    }

    cp = _load_checkpoint()
    if cp:
        print("Found checkpoint (%s): %d items, %d queue" % (
            cp.get("saved_at", "?"), len(cp["items"]), len(cp["queue"])), flush=True)
        crawled = deep_crawl(CFG.root_page_id, resume_items=cp["items"], resume_queue=cp["queue"])
    else:
        crawled = deep_crawl(CFG.root_page_id)

    items = [root_item] + crawled
    _mark_containers(items)

    CFG.sync_dir.mkdir(parents=True, exist_ok=True)
    with open(CFG.tree_json, "w") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)

    pages = [i for i in items if i["type"] == "page"]
    dbs = [i for i in items if i["type"] == "db"]
    print("Found: %d pages (%d + root), %d databases" % (len(pages), len(pages) - 1, len(dbs)), flush=True)
    print("Saved to: %s" % CFG.tree_json, flush=True)

    top_level = [i for i in items if i["depth"] == 0]
    print("\nTop-level children:", flush=True)
    for item in top_level:
        print("  [%s] %s" % (item["type"], item["title"]), flush=True)

    return items


def cmd_sync(force=False, dry_run=False, refresh=False):
    if refresh or not CFG.tree_json.exists():
        if not CFG.tree_json.exists():
            print("No tree cache found. Running refresh...", flush=True)
        tree = cmd_crawl()
    else:
        with open(CFG.tree_json) as f:
            tree = json.load(f)

    state = load_sync_state()
    now = datetime.now().isoformat()

    new_items = []
    updated_items = []
    unchanged = 0
    errors = 0

    print("=== Differential Sync [%s] ===" % CFG.label, flush=True)
    print("Started: " + now, flush=True)
    print("Total items: %d" % len(tree), flush=True)
    print("Checking for changes...", flush=True)
    print(flush=True)

    for idx, item in enumerate(tree):
        item_id = item["id"]

        if item_id not in state["items"]:
            new_items.append(item)
            continue

        if force:
            updated_items.append(item)
            continue

        if item["type"] == "page":
            remote_edited = get_page_last_edited(item_id)
            local_synced = state["items"][item_id].get("last_edited_time", "")
            if remote_edited and remote_edited != local_synced:
                updated_items.append(item)
                print("[%d/%d] Changed: %s" % (idx + 1, len(tree), item["title"][:60]), flush=True)
            else:
                unchanged += 1
            time.sleep(CFG.rate_limit_delay)
        else:
            updated_items.append(item)

        if (idx + 1) % 50 == 0:
            print("  ... checked %d/%d" % (idx + 1, len(tree)), flush=True)

    print(flush=True)
    print("New: %d, Updated: %d, Unchanged: %d" % (len(new_items), len(updated_items), unchanged), flush=True)

    to_download = new_items + updated_items
    if not to_download:
        print("Nothing to sync.", flush=True)
        return

    if dry_run:
        print(flush=True)
        print("Would download %d items:" % len(to_download), flush=True)
        for idx, item in enumerate(to_download):
            label = "NEW" if item in new_items else "UPD"
            print("  [%d] %s %s %s" % (
                idx + 1, label,
                "db" if item["type"] == "db" else "pg",
                item["title"][:60]
            ), flush=True)
        print("\n(dry-run: no files modified)", flush=True)
        return

    print(flush=True)
    print("Downloading %d items..." % len(to_download), flush=True)

    success = 0
    for idx, item in enumerate(to_download):
        label = "NEW" if item in new_items else "UPD"
        print("[%d/%d] %s %s %s" % (
            idx + 1, len(to_download), label,
            "db" if item["type"] == "db" else "pg",
            item["title"][:60]
        ), flush=True)

        ok = download_item(item)
        if ok:
            success += 1
        else:
            errors += 1

        remote_edited = ""
        if item["type"] == "page":
            remote_edited = get_page_last_edited(item["id"])
            time.sleep(CFG.rate_limit_delay)

        state["items"][item["id"]] = {
            "title": item["title"], "path": item["path"],
            "type": item["type"], "last_edited_time": remote_edited,
            "synced_at": datetime.now().isoformat(),
        }
        save_sync_state(state)
        time.sleep(CFG.rate_limit_delay)

    state["last_sync"] = now
    save_sync_state(state)

    print(flush=True)
    print("=== Sync Complete ===", flush=True)
    print("Success: %d, Errors: %d (New: %d, Updated: %d)" % (
        success, errors, len(new_items), len(updated_items)), flush=True)
    print("Finished: " + datetime.now().isoformat(), flush=True)


def cmd_init_state():
    if not CFG.tree_json.exists():
        print("No tree cache found. Run 'crawl' first.", flush=True)
        return

    with open(CFG.tree_json) as f:
        tree = json.load(f)

    state = {"items": {}, "last_full_crawl": None}
    found = 0
    missing = 0

    for item in tree:
        is_present = False
        filepath = item_to_filepath(item)
        if item["type"] == "page" and filepath and filepath.exists():
            is_present = True
        elif item["type"] == "db":
            is_present = db_filepath(item["title"], item.get("path", "")).exists()

        if is_present:
            state["items"][item["id"]] = {
                "title": item["title"], "path": item["path"],
                "type": item["type"], "last_edited_time": "",
                "synced_at": datetime.now().isoformat(),
            }
            found += 1
        else:
            missing += 1

    state["last_sync"] = datetime.now().isoformat()
    save_sync_state(state)
    print("Initialized: %d items tracked, %d missing" % (found, missing), flush=True)


def cmd_status():
    state = load_sync_state()
    items = state.get("items", {})
    last_sync = state.get("last_sync", "never")

    print("=== Sync Status [%s] ===" % CFG.label, flush=True)
    print("Root: %s" % CFG.root_page_id, flush=True)
    print("Output: %s" % CFG.base_output_dir, flush=True)
    print("Tracked items: %d" % len(items), flush=True)
    print("Last sync: %s" % last_sync, flush=True)

    pages = {k: v for k, v in items.items() if v.get("type") == "page"}
    dbs = {k: v for k, v in items.items() if v.get("type") == "db"}
    print("  Pages: %d, Databases: %d" % (len(pages), len(dbs)), flush=True)

    if CFG.tree_json.exists():
        with open(CFG.tree_json) as f:
            tree = json.load(f)
        tree_ids = set(i["id"] for i in tree)
        tracked_ids = set(items.keys())
        new_in_tree = tree_ids - tracked_ids
        removed = tracked_ids - tree_ids
        print("Tree cache: %d items" % len(tree), flush=True)
        if new_in_tree:
            print("Unsynced (new on Notion): %d" % len(new_in_tree), flush=True)
        if removed:
            print("Removed from Notion: %d" % len(removed), flush=True)
    else:
        print("Tree cache: not found (run 'crawl')", flush=True)

    db_files = sorted(CFG.base_output_dir.rglob("*.db"))
    excl = CFG.exclude_paths
    db_files = [f for f in db_files if not any(e in str(f) for e in excl) and f.name != ".DS_Store"]
    if db_files:
        print("\nSQLite databases:", flush=True)
        for dbf in db_files:
            try:
                conn = sqlite3.connect(str(dbf))
                c = conn.cursor()
                c.execute("SELECT value FROM _metadata WHERE key='row_count'")
                row = c.fetchone()
                cnt = row[0] if row else "?"
                c.execute("SELECT value FROM _metadata WHERE key='synced_at'")
                row = c.fetchone()
                synced = row[0][:19] if row else "?"
                conn.close()
                rel = dbf.relative_to(CFG.base_output_dir)
                print("  %s  (%s rows, synced: %s)" % (rel, cnt, synced), flush=True)
            except Exception:
                pass

    md_count = len(list(CFG.base_output_dir.rglob("*.md")))
    print("\nLocal .md files: %d" % md_count, flush=True)


def _find_all_dbs():
    all_dbs = sorted(CFG.base_output_dir.rglob("*.db"))
    excl = CFG.exclude_paths
    return [f for f in all_dbs if not any(e in str(f) for e in excl) and f.name != ".DS_Store"]


def _find_db_file(db_name):
    if not db_name.endswith(".db"):
        db_name += ".db"
    for dbf in _find_all_dbs():
        if dbf.name == db_name:
            return dbf
    return None


def cmd_db_query(db_name, sql):
    db_path = _find_db_file(db_name)
    if not db_path:
        print("ERROR: '%s' not found." % db_name, flush=True)
        print("Available databases:", flush=True)
        for f in _find_all_dbs():
            print("  %s" % f.name, flush=True)
        return

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    try:
        c.execute(sql)
        rows = c.fetchall()
        if rows:
            headers = rows[0].keys()
            print(" | ".join(headers), flush=True)
            print("-" * (len(headers) * 20), flush=True)
            for row in rows:
                print(" | ".join(str(row[h]) for h in headers), flush=True)
            print("\n(%d rows)" % len(rows), flush=True)
        else:
            print("(0 rows)", flush=True)
    except sqlite3.Error as e:
        print("SQL Error: %s" % e, flush=True)
    finally:
        conn.close()


def cmd_db_list():
    db_files = _find_all_dbs()
    if not db_files:
        print("No .db files found.", flush=True)
        return
    print("=== Notion Databases (SQLite) [%s] ===" % CFG.label, flush=True)
    for dbf in db_files:
        try:
            conn = sqlite3.connect(str(dbf))
            c = conn.cursor()
            meta = {}
            c.execute("SELECT key, value FROM _metadata")
            for k, v in c.fetchall():
                meta[k] = v
            conn.close()
            rel = dbf.relative_to(CFG.base_output_dir)
            print("  %s" % rel, flush=True)
            print("    Title: %s  |  Rows: %s  |  Synced: %s" % (
                meta.get("db_title", "?"), meta.get("row_count", "?"),
                meta.get("synced_at", "?")[:19]
            ), flush=True)
            print("", flush=True)
        except Exception:
            print("  %s (read error)" % dbf.name, flush=True)


# ==========================================
# Init: Create new workspace
# ==========================================

def _find_skill_dir():
    """Discover the nsync skill directory (contains .env and scripts/)."""
    script_dir = Path(__file__).resolve().parent
    candidate = script_dir.parent
    if (candidate / "SKILL.md").exists() or (candidate / "scripts").is_dir():
        return candidate
    return None


def _load_token_from_env_files():
    """Load NOTION_API_TOKEN from .env files with fallback chain."""
    token = os.environ.get("NOTION_API_TOKEN", "")
    if token:
        return token
    skill_dir = _find_skill_dir()
    if skill_dir:
        env_path = skill_dir / ".env"
        if env_path.exists():
            for line in env_path.read_text().split("\n"):
                if line.startswith("NOTION_API_TOKEN="):
                    token = line.split("=", 1)[1].strip()
                    if token:
                        return token
    return ""


def _fetch_page_title(page_id):
    """Notion API でページタイトルを取得（init 用）"""
    token = _load_token_from_env_files()
    if not token:
        return ""
    headers = {
        "Authorization": "Bearer " + token,
        "Notion-Version": os.environ.get("NOTION_API_VERSION", "2022-06-28"),
    }
    url = "https://api.notion.com/v1/pages/%s" % page_id
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        props = data.get("properties", {})
        for val in props.values():
            if val.get("type") == "title":
                parts = val.get("title", [])
                return "".join(t.get("plain_text", "") for t in parts)
    except Exception:
        pass
    return ""


def cmd_init_workspace(notion_url, output_dir):
    page_id = extract_page_id_from_url(notion_url)
    if not page_id:
        print("ERROR: Could not extract page ID from URL: %s" % notion_url, flush=True)
        sys.exit(1)

    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    sync_dir = out / "_sync"
    sync_dir.mkdir(exist_ok=True)

    notion_title = _fetch_page_title(page_id)
    label = notion_title if notion_title else out.name
    if notion_title:
        print("Notion page title: %s" % notion_title, flush=True)

    config_path = out / ".nsync.yaml"
    config_content = """root_page_id: "%s"
label: "%s"
crawl_max_depth: 10
rate_limit_delay: 0.35
exclude_paths:
  - "_sync"
  - "_archived"
""" % (page_id, label)
    config_path.write_text(config_content, encoding="utf-8")
    print("Created: %s" % config_path, flush=True)

    env_path = sync_dir / ".env"
    if not env_path.exists():
        skill_dir = _find_skill_dir()
        shared_env = skill_dir / ".env" if skill_dir else None
        if shared_env and shared_env.exists():
            import shutil
            shutil.copy2(str(shared_env), str(env_path))
            print("Copied token from: %s" % shared_env, flush=True)
        else:
            env_path.write_text("NOTION_API_TOKEN=\nNOTION_API_VERSION=2022-06-28\n", encoding="utf-8")
            print("Created: %s  (fill in your token!)" % env_path, flush=True)
    else:
        print("Exists: %s" % env_path, flush=True)

    wrapper_path = out / "nsync.sh"
    wrapper_content = r"""#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
# Discover nsync.py
NSYNC="${NSYNC_SCRIPT:-}"
if [ -z "$NSYNC" ]; then
  GIT_ROOT="$(git -C "$DIR" rev-parse --show-toplevel 2>/dev/null)"
  for candidate in \
    "${GIT_ROOT:-.}/.claude/skills/nsync/scripts/nsync.py" \
    "$HOME/.claude/skills/nsync/scripts/nsync.py"; do
    [ -f "$candidate" ] && NSYNC="$candidate" && break
  done
fi
if [ -z "$NSYNC" ]; then
  echo "ERROR: nsync.py not found. Set NSYNC_SCRIPT or install to .claude/skills/nsync/"
  exit 1
fi
SKILL_DIR="$(dirname "$(dirname "$NSYNC")")"
if [ -f "$DIR/_sync/.env" ]; then set -a; . "$DIR/_sync/.env"; set +a; fi
if [ -z "$NOTION_API_TOKEN" ] && [ -f "$SKILL_DIR/.env" ]; then set -a; . "$SKILL_DIR/.env"; set +a; fi
[ -z "$NOTION_API_TOKEN" ] && echo "ERROR: NOTION_API_TOKEN not set." && exit 1
python3 "$NSYNC" --config "$DIR/.nsync.yaml" "${@:-sync}"
"""
    wrapper_path.write_text(wrapper_content, encoding="utf-8")
    os.chmod(str(wrapper_path), 0o755)
    print("Created: %s" % wrapper_path, flush=True)

    print("\n=== nsync workspace initialized ===", flush=True)
    print("Root page: %s" % page_id, flush=True)
    print("Output: %s" % out, flush=True)
    print("\nNext steps:", flush=True)
    print("  1. Edit %s and set NOTION_API_TOKEN" % env_path, flush=True)
    print("  2. cd %s" % out, flush=True)
    print("  3. ./nsync.sh sync", flush=True)


# ==========================================
# Main
# ==========================================

def main():
    args = sys.argv[1:]

    if not args or args[0] in ("--help", "-h"):
        print_usage()
        return

    if args[0] == "--version":
        print("nsync %s" % __version__)
        return

    config_path = None
    if args[0] == "--config" and len(args) >= 2:
        config_path = args[1]
        args = args[2:]

    command = args[0] if args else "sync"

    if command in ("--help", "-h"):
        print_usage()
        return

    if command == "init":
        if len(args) < 2:
            print("Usage: nsync.py init <notion-url> [output-dir]", flush=True)
            sys.exit(1)
        notion_url = args[1]
        output_dir = args[2] if len(args) >= 3 else "."
        cmd_init_workspace(notion_url, output_dir)
        return

    if not config_path:
        for candidate in [".nsync.yaml", "_sync/.nsync.yaml"]:
            if Path(candidate).exists():
                config_path = candidate
                break
        if not config_path:
            print("ERROR: No .nsync.yaml found. Run 'nsync.py init <url> <dir>' first.", flush=True)
            sys.exit(1)

    load_config(config_path)
    init_api()

    LOCAL_ONLY_CMDS = ("query", "db-list", "status", "init-state")
    if command not in LOCAL_ONLY_CMDS and not TOKEN:
        print("ERROR: NOTION_API_TOKEN not set", flush=True)
        sys.exit(1)

    if command == "crawl":
        cmd_crawl()
    elif command == "sync":
        force = "--force" in args
        dry_run = "--dry-run" in args
        refresh = "--refresh" in args
        full = "--full" in args
        if full:
            refresh = True
            force = True
        cmd_sync(force=force, dry_run=dry_run, refresh=refresh)
    elif command == "init-state":
        cmd_init_state()
    elif command == "status":
        cmd_status()
    elif command == "full":
        cmd_sync(force=True, refresh=True)
    elif command == "pull":
        dry_run = "--dry-run" in args
        pull_args = [a for a in args[1:] if a != "--dry-run"]
        if len(pull_args) < 1:
            print("Usage: nsync.py pull [--dry-run] <filepath.md>", flush=True)
            sys.exit(1)
        cmd_pull(pull_args[0], dry_run=dry_run)
    elif command == "push":
        dry_run = "--dry-run" in args
        push_args = [a for a in args[1:] if a != "--dry-run"]
        if len(push_args) < 1:
            print("Usage: nsync.py push [--dry-run] <filepath.md>", flush=True)
            sys.exit(1)
        cmd_push(push_args[0], dry_run=dry_run)
    elif command == "query":
        if len(args) < 3:
            print('Usage: nsync.py query <db_name> "SELECT ..."', flush=True)
            sys.exit(1)
        cmd_db_query(args[1], " ".join(args[2:]))
    elif command == "db-list":
        cmd_db_list()
    else:
        print_usage()
        sys.exit(1)


def print_usage():
    print("nsync %s - Notion Sync Tool" % __version__, flush=True)
    print("", flush=True)
    print("Usage:", flush=True)
    print("  nsync.py init <notion-url> [output-dir]    Setup new workspace", flush=True)
    print("  nsync.py --config <.nsync.yaml> <command>  Run with config", flush=True)
    print("", flush=True)
    print("Commands:", flush=True)
    print("  sync               Differential sync (default)", flush=True)
    print("  sync --refresh     Refresh page list, then sync", flush=True)
    print("  sync --force       Force re-download all pages", flush=True)
    print("  sync --full         Refresh + force (complete re-sync)", flush=True)
    print("  sync --dry-run     Show what would be synced", flush=True)
    print("  pull <file.md>     Pull single page from Notion", flush=True)
    print("  push <file.md>     Push local MD to Notion", flush=True)
    print("  status             Show sync status", flush=True)
    print("  init-state         Init state from existing files", flush=True)
    print("  db-list            List all databases", flush=True)
    print('  query <db> "SQL"   Query a database', flush=True)


if __name__ == "__main__":
    main()
