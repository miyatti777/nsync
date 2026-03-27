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

import atexit
import hashlib
import json
import os
import re
import signal
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
        self.db_page_content = True

CFG = Config()

TOKEN = ""
API_VERSION = ""
HEADERS = {}

_api_stats = {"calls": 0, "rate_limits": 0, "errors": 0, "skipped": 0}


class RateLimitExhausted(Exception):
    """Raised when too many consecutive 429s indicate we should stop."""
    pass


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
    CFG.db_page_content = data.get("db_page_content", True)


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
# Crash Detection & Heartbeat
# ==========================================

_crawl_state = {"items": None, "queue": None, "active": False}

def _heartbeat_path():
    return Path(CFG.base_output_dir) / "_sync" / "heartbeat"

def _write_heartbeat():
    """Write current timestamp to heartbeat file for external monitoring."""
    try:
        hb = _heartbeat_path()
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.write_text(datetime.now().isoformat() + "\n")
    except Exception:
        pass

def _remove_heartbeat():
    try:
        _heartbeat_path().unlink(missing_ok=True)
    except Exception:
        pass

def _emergency_save():
    """Save checkpoint on crash/signal if crawl is active."""
    if _crawl_state["active"] and _crawl_state["items"] is not None:
        try:
            _save_checkpoint(_crawl_state["items"], _crawl_state["queue"] or [])
            print("\n  [emergency checkpoint saved on exit: %d items]" %
                  len(_crawl_state["items"]), flush=True)
        except Exception:
            pass
    _remove_heartbeat()
    _print_api_stats()

def _signal_handler(signum, frame):
    sig_name = signal.Signals(signum).name if hasattr(signal, 'Signals') else str(signum)
    print("\n  Received %s — saving state..." % sig_name, flush=True)
    _emergency_save()
    sys.exit(128 + signum)

signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)
atexit.register(_emergency_save)


# ==========================================
# Notion API
# ==========================================

def _api_request(url, method="GET", body_dict=None, retries=None):
    """Unified API request with Retry-After support and rate limit tracking."""
    if retries is None:
        retries = CFG.max_retries
    data_bytes = json.dumps(body_dict).encode() if body_dict else None
    consecutive_429 = 0

    for attempt in range(retries):
        req = urllib.request.Request(url, data=data_bytes, headers=HEADERS, method=method)
        _api_stats["calls"] += 1
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                _api_stats["rate_limits"] += 1
                consecutive_429 += 1
                retry_after = e.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = max(float(retry_after), 1.0)
                    except ValueError:
                        wait = CFG.retry_backoff ** (attempt + 1)
                else:
                    wait = CFG.retry_backoff ** (attempt + 1)
                print("  Rate limited (429 #%d). Waiting %.1fs..." % (
                    _api_stats["rate_limits"], wait), flush=True)
                if consecutive_429 >= retries:
                    _api_stats["errors"] += 1
                    raise RateLimitExhausted(
                        "Too many consecutive 429s (%d). Stopping to avoid API ban." % consecutive_429)
                time.sleep(wait)
                continue
            elif e.code == 404:
                return None
            else:
                _api_stats["errors"] += 1
                print("  HTTP %d on attempt %d: %s" % (e.code, attempt + 1, url), flush=True)
                if attempt < retries - 1:
                    time.sleep(CFG.retry_backoff ** attempt)
                    continue
                return None
        except RateLimitExhausted:
            raise
        except Exception as e:
            _api_stats["errors"] += 1
            print("  Error on attempt %d: %s" % (attempt + 1, e), flush=True)
            if attempt < retries - 1:
                time.sleep(CFG.retry_backoff ** attempt)
                continue
            return None
    return None


def api_get(url, retries=None):
    return _api_request(url, "GET", retries=retries)


def api_post(url, body_dict, retries=None):
    return _api_request(url, "POST", body_dict, retries=retries)


def api_patch(url, body_dict, retries=None):
    return _api_request(url, "PATCH", body_dict, retries=retries)


def api_delete(url, retries=None):
    return _api_request(url, "DELETE", retries=retries)


# ==========================================
# File Upload API (requires Notion-Version >= 2026-03-11)
# ==========================================

_FILE_UPLOAD_API_VERSION = "2026-03-11"

_MIME_MAP = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
    ".tiff": "image/tiff", ".bmp": "image/bmp", ".heic": "image/heic",
    ".pdf": "application/pdf",
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska", ".webm": "video/webm",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".flac": "audio/flac", ".aac": "audio/aac", ".m4a": "audio/mp4",
}


def _upload_file_to_notion(local_path, purpose="block_content"):
    """Upload a local file to Notion using the File Upload API (3-step).
    Returns file_upload_id on success, None on failure."""
    local_path = Path(local_path)
    if not local_path.exists():
        print("  ERROR: File not found for upload: %s" % local_path, flush=True)
        return None

    file_size = local_path.stat().st_size
    if file_size > 20 * 1024 * 1024:
        print("  WARN: File >20MB, skipping (multi-part not yet supported): %s" % local_path.name, flush=True)
        return None

    ext = local_path.suffix.lower()
    content_type = _MIME_MAP.get(ext, "application/octet-stream")

    upload_headers = {
        "Authorization": "Bearer " + TOKEN,
        "Notion-Version": _FILE_UPLOAD_API_VERSION,
        "Content-Type": "application/json",
    }

    # Step 1: Create file upload object
    create_body = json.dumps({
        "mode": "single_part",
        "filename": local_path.name,
        "content_type": content_type,
        "purpose": purpose,
    }).encode()

    req = urllib.request.Request(
        "https://api.notion.com/v1/file-uploads",
        data=create_body, headers=upload_headers, method="POST"
    )
    _api_stats["calls"] += 1
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            create_resp = json.loads(resp.read())
    except Exception as e:
        print("  ERROR: File upload create failed: %s" % e, flush=True)
        _api_stats["errors"] += 1
        return None

    file_upload_id = create_resp.get("id")
    if not file_upload_id:
        print("  ERROR: No file_upload_id in response", flush=True)
        return None

    # Step 2: Send binary data (multipart/form-data)
    import uuid as _uuid
    boundary = "----nsync" + _uuid.uuid4().hex[:16]
    file_data = local_path.read_bytes()
    body_parts = []
    body_parts.append(("--%s\r\n" % boundary).encode())
    body_parts.append(('Content-Disposition: form-data; name="file"; filename="%s"\r\n' % local_path.name).encode())
    body_parts.append(("Content-Type: %s\r\n\r\n" % content_type).encode())
    body_parts.append(file_data)
    body_parts.append(("\r\n--%s--\r\n" % boundary).encode())
    multipart_body = b"".join(body_parts)

    send_headers = {
        "Authorization": "Bearer " + TOKEN,
        "Notion-Version": _FILE_UPLOAD_API_VERSION,
        "Content-Type": "multipart/form-data; boundary=%s" % boundary,
    }
    send_url = "https://api.notion.com/v1/file-uploads/%s/send" % file_upload_id
    req2 = urllib.request.Request(send_url, data=multipart_body, headers=send_headers, method="POST")
    _api_stats["calls"] += 1
    try:
        with urllib.request.urlopen(req2, timeout=120) as resp:
            json.loads(resp.read())
    except Exception as e:
        print("  ERROR: File upload send failed: %s" % e, flush=True)
        _api_stats["errors"] += 1
        return None

    print("  Uploaded: %s (%s)" % (local_path.name, content_type), flush=True)
    return file_upload_id


def _make_file_upload_block(file_upload_id, btype="image", caption_rt=None):
    """Create a Notion block referencing an uploaded file."""
    block = {
        "object": "block",
        "type": btype,
        btype: {
            "type": "file_upload",
            "file_upload": {"id": file_upload_id},
        }
    }
    if caption_rt:
        block[btype]["caption"] = caption_rt
    return block


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
    processed = 0
    initial_queue = len(queue)
    last_checkpoint_time = time.time()

    _crawl_state["items"] = items
    _crawl_state["queue"] = queue
    _crawl_state["active"] = True

    try:
        while queue:
            current_id, current_path, current_depth = queue.pop(0)
            processed += 1

            _write_heartbeat()
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

            if processed % 50 == 0:
                print("  [processed %d queue items, found %d total, %d remaining]" % (processed, count, len(queue)), flush=True)

            now_t = time.time()
            if now_t - last_checkpoint_time >= 60:
                _save_checkpoint(items, queue)
                last_checkpoint_time = now_t
                if processed % 50 != 0:
                    print("  [time-checkpoint: %d items, %d remaining]" % (count, len(queue)), flush=True)

            time.sleep(CFG.rate_limit_delay)
    except RateLimitExhausted as e:
        print("  RATE LIMIT: %s" % e, flush=True)
        _save_checkpoint(items, queue)
        print("  [emergency checkpoint saved: %d items, %d remaining in queue]" % (count, len(queue)), flush=True)
        print("  Re-run the same command to resume from checkpoint.", flush=True)
        raise

    _crawl_state["active"] = False
    _remove_checkpoint()
    _remove_heartbeat()
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
    if btype == "child_page":
        title = b.get("child_page", {}).get("title", "")
        return [(btype, "[[📄 %s]]" % title)]
    if btype == "child_database":
        title = b.get("child_database", {}).get("title", "")
        return [(btype, "[[🗃️ %s]]" % title)]
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
    elif btype == "heading_4":
        md_line = "#### " + text
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
    elif btype == "pdf":
        pdf_info = block_data
        pdf_url = ""
        if pdf_info.get("type") == "file":
            pdf_url = pdf_info.get("file", {}).get("url", "")
        elif pdf_info.get("type") == "external":
            pdf_url = pdf_info.get("external", {}).get("url", "")
        caption_rt = pdf_info.get("caption", [])
        caption = rich_text_to_markdown(caption_rt) if caption_rt else "PDF"
        if pdf_url:
            md_line = "[📎 %s](%s)" % (caption, pdf_url)
    elif btype == "video":
        vid_info = block_data
        vid_url = ""
        if vid_info.get("type") == "file":
            vid_url = vid_info.get("file", {}).get("url", "")
        elif vid_info.get("type") == "external":
            vid_url = vid_info.get("external", {}).get("url", "")
        caption_rt = vid_info.get("caption", [])
        caption = rich_text_to_markdown(caption_rt) if caption_rt else "Video"
        if vid_url:
            md_line = "[🎬 %s](%s)" % (caption, vid_url)
    elif btype == "audio":
        aud_info = block_data
        aud_url = ""
        if aud_info.get("type") == "file":
            aud_url = aud_info.get("file", {}).get("url", "")
        elif aud_info.get("type") == "external":
            aud_url = aud_info.get("external", {}).get("url", "")
        caption_rt = aud_info.get("caption", [])
        caption = rich_text_to_markdown(caption_rt) if caption_rt else "Audio"
        if aud_url:
            md_line = "[🔊 %s](%s)" % (caption, aud_url)
    elif btype == "file":
        file_info = block_data
        file_url = ""
        if file_info.get("type") == "file":
            file_url = file_info.get("file", {}).get("url", "")
        elif file_info.get("type") == "external":
            file_url = file_info.get("external", {}).get("url", "")
        caption_rt = file_info.get("caption", [])
        caption = rich_text_to_markdown(caption_rt) if caption_rt else "File"
        if file_url:
            md_line = "[📁 %s](%s)" % (caption, file_url)
    elif btype == "equation":
        expr = block_data.get("expression", "")
        if expr:
            md_line = "$$%s$$" % expr
    elif btype == "table":
        table_lines = fetch_table_as_markdown(b["id"])
        md_line = "\n".join(table_lines)
    elif btype == "paragraph" and plain.startswith("▸ ") and rt and len(rt) >= 2 and rt[1].get("annotations", {}).get("bold"):
        # Styled H5: ▸ **Title** → ##### Title
        h5_text = rich_text_to_markdown(rt[1:])
        if h5_text.startswith("**") and h5_text.endswith("**"):
            h5_text = h5_text[2:-2]
        md_line = "##### " + h5_text
    elif btype == "paragraph" and plain.startswith("▹ ") and rt and len(rt) >= 2 and rt[1].get("annotations", {}).get("bold"):
        # Styled H6: ▹ **Title** → ###### Title
        h6_text = rich_text_to_markdown(rt[1:])
        if h6_text.startswith("**") and h6_text.endswith("**"):
            h6_text = h6_text[2:-2]
        md_line = "###### " + h6_text
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


def _extract_assets_from_blocks(blocks):
    """Extract downloadable asset metadata from raw Notion blocks.
    Returns list of dicts: {block_id, url, filename, btype}."""
    assets = []
    _MEDIA_BTYPES = ("image", "pdf", "video", "audio", "file")
    for b in blocks:
        btype = b["type"]
        if btype not in _MEDIA_BTYPES:
            continue
        block_data = b.get(btype, {})
        if block_data.get("type") != "file":
            continue
        file_url = block_data.get("file", {}).get("url", "")
        if not file_url:
            continue
        block_id = b["id"].replace("-", "")[:12]
        fname = _asset_filename_from_url(file_url, block_id, btype)
        assets.append({"block_id": b["id"], "url": file_url, "filename": fname, "btype": btype})
    return assets


def _asset_filename_from_url(url, block_id_short, btype):
    """Derive a local filename from the Notion S3 URL."""
    from urllib.parse import urlparse, unquote
    parsed = urlparse(url)
    path_parts = parsed.path.rsplit("/", 1)
    if len(path_parts) > 1:
        raw_name = unquote(path_parts[-1])
    else:
        _EXT_MAP = {"image": ".png", "pdf": ".pdf", "video": ".mp4", "audio": ".mp3", "file": ".bin"}
        raw_name = block_id_short + _EXT_MAP.get(btype, ".bin")
    return block_id_short + "_" + re.sub(r'[^\w.\-]', '_', raw_name)


def _download_file(url, dest_path):
    """Download a file from URL to local path. Returns True on success."""
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=60) as resp:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with open(dest_path, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
        return True
    except Exception as e:
        print("  WARN: Failed to download asset: %s" % e, flush=True)
        return False


def _download_page_assets(assets, page_dir):
    """Download assets to page_dir/_assets/. Returns dict mapping url -> relative path.
    Reuses existing files with same original name to prevent bloat from push→pull cycles."""
    if not assets:
        return {}
    assets_dir = page_dir / "_assets"
    url_to_relpath = {}

    existing_by_suffix = {}
    if assets_dir.exists():
        for f in assets_dir.iterdir():
            if f.is_file() and not f.name.startswith("."):
                parts = f.name.split("_", 1)
                if len(parts) > 1:
                    existing_by_suffix[parts[1]] = f

    for a in assets:
        dest = assets_dir / a["filename"]
        if dest.exists():
            url_to_relpath[a["url"]] = "_assets/" + a["filename"]
            continue

        # Check if same original file exists under different block_id prefix
        suffix = a["filename"].split("_", 1)[-1] if "_" in a["filename"] else a["filename"]
        existing = existing_by_suffix.get(suffix)
        if existing and existing.exists():
            url_to_relpath[a["url"]] = "_assets/" + existing.name
            continue

        if _download_file(a["url"], dest):
            url_to_relpath[a["url"]] = "_assets/" + a["filename"]
            existing_by_suffix[suffix] = dest
            print("    Asset: %s" % a["filename"], flush=True)
    return url_to_relpath


def _replace_asset_urls(md_text, url_map):
    """Replace Notion S3 URLs in markdown text with local relative paths."""
    for url, local_path in url_map.items():
        md_text = md_text.replace(url, local_path)
    return md_text


def fetch_page_blocks_as_text(page_id, page_dir=None):
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

    # Extract and download assets if page_dir provided
    assets = _extract_assets_from_blocks(blocks)
    url_map = {}
    if page_dir and assets:
        url_map = _download_page_assets(assets, page_dir)

    COMPACT_TYPES = ("bulleted_list_item", "numbered_list_item", "to_do", "toggle",
                      "child_page", "child_database")
    entries = []
    for b in blocks:
        entries.extend(_block_to_md(b, depth=0))

    result_parts = []
    for i, (btype, md_line) in enumerate(entries):
        if i == 0:
            result_parts.append(md_line)
            continue
        prev_type = entries[i - 1][0]
        both_compact = prev_type in COMPACT_TYPES and btype in COMPACT_TYPES
        if both_compact:
            result_parts.append("\n" + md_line)
        else:
            result_parts.append("\n\n" + md_line)

    md_text = "".join(result_parts)
    if url_map:
        md_text = _replace_asset_urls(md_text, url_map)
    return md_text


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
    elif ptype == "_files_with_download":
        # Internal: used when db_assets_dir is set for downloading
        return prop.get("_local_paths", "")
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


def _detect_multi_datasource(db_id):
    """Check if a DB is multi-data-source by retrieving metadata with newer API."""
    url = "https://api.notion.com/v1/databases/%s" % db_id
    headers_v2 = dict(HEADERS)
    headers_v2["Notion-Version"] = "2025-09-03"
    req = urllib.request.Request(url, headers=headers_v2)
    _api_stats["calls"] += 1
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        sources = data.get("data_sources", [])
        if sources:
            return sources
    except Exception:
        pass
    return []


def _fetch_multi_datasource_rows(data_sources):
    """Fetch rows from all data sources using v2025-09-03 API."""
    all_rows = []
    all_props = set()
    headers_v2 = dict(HEADERS)
    headers_v2["Notion-Version"] = "2025-09-03"

    for ds in data_sources:
        ds_id = ds.get("data_source_id", ds.get("id", ""))
        if not ds_id:
            continue
        cursor = None
        ds_count = 0
        while True:
            payload = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            data_bytes = json.dumps(payload).encode()
            url = "https://api.notion.com/v1/data_sources/%s/query" % ds_id
            req = urllib.request.Request(url, data=data_bytes, headers=headers_v2, method="POST")
            _api_stats["calls"] += 1
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read())
            except Exception as e:
                print("    data_source %s error: %s" % (ds_id[:12], e), flush=True)
                break

            for page in data.get("results", []):
                props = page.get("properties", {})
                row = {"_notion_page_id": page["id"], "_data_source": ds_id}
                for key, val in props.items():
                    all_props.add(key)
                    row[key] = extract_property_value(val)
                row["_created_time"] = page.get("created_time", "")
                row["_last_edited_time"] = page.get("last_edited_time", "")
                all_rows.append(row)
                ds_count += 1

            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            time.sleep(CFG.rate_limit_delay)

        print("    data_source %s: %d rows" % (ds_id[:12], ds_count), flush=True)

    return all_rows, all_props


def _download_db_file_assets(rows, columns, db_assets_dir):
    """Download file-type property files for DB rows to local _db_assets/ dir."""
    from urllib.parse import urlparse, unquote
    for row in rows:
        page_id_short = row.get("_notion_page_id", "")[:12].replace("-", "")
        for col in columns:
            val = row.get(col, "")
            if not val or not isinstance(val, str):
                continue
            urls = [u.strip() for u in val.split(", ")]
            has_s3 = any("amazonaws.com" in u or "notion-static.com" in u for u in urls)
            if not has_s3:
                continue
            local_paths = []
            for url in urls:
                if "amazonaws.com" not in url and "notion-static.com" not in url:
                    local_paths.append(url)
                    continue
                fname = _asset_filename_from_url(url, page_id_short, "file")
                dest = db_assets_dir / fname
                if not dest.exists():
                    if _download_file(url, dest):
                        print("    DB Asset: %s" % fname, flush=True)
                if dest.exists():
                    local_paths.append("_db_assets/" + fname)
                else:
                    local_paths.append(url)
            row[col] = ", ".join(local_paths)


def fetch_database_to_sqlite(db_id, db_title, db_path):
    all_rows = []
    all_props = set()
    cursor = None
    is_multi_ds = False

    while True:
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        data = api_post("https://api.notion.com/v1/databases/%s/query" % db_id, payload)
        if not data:
            sources = _detect_multi_datasource(db_id)
            if sources:
                print("    Multi-data-source DB detected (%d sources)" % len(sources), flush=True)
                all_rows, all_props = _fetch_multi_datasource_rows(sources)
                is_multi_ds = True
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

    # Download file assets from files properties
    db_assets_dir = sqlite_path.parent / "_db_assets"
    _download_db_file_assets(all_rows, columns, db_assets_dir)

    conn = sqlite3.connect(str(sqlite_path))
    c = conn.cursor()

    c.execute("DROP TABLE IF EXISTS data")

    col_defs = ["_notion_page_id TEXT PRIMARY KEY"]
    if is_multi_ds:
        col_defs.append("_data_source TEXT")
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
        if is_multi_ds:
            vals.append(row.get("_data_source", ""))
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
    meta_items = [
        ("notion_db_id", db_id), ("notion_db_path", db_path),
        ("db_title", db_title), ("row_count", str(len(all_rows))),
        ("synced_at", now),
    ]
    if is_multi_ds:
        meta_items.append(("multi_data_source", "true"))
    for k, v in meta_items:
        c.execute("INSERT OR REPLACE INTO _metadata VALUES (?, ?)", (k, v))

    conn.commit()
    conn.close()
    return len(all_rows)


def _read_db_metadata(db_path):
    """Read _metadata table from a .db file."""
    conn = sqlite3.connect(str(db_path))
    c = conn.cursor()
    try:
        c.execute("SELECT key, value FROM _metadata")
        meta = dict(c.fetchall())
    except sqlite3.Error:
        meta = {}
    conn.close()
    return meta


def fetch_db_schema(db_id):
    """Fetch property name -> type mapping from Notion database schema."""
    data = api_get("https://api.notion.com/v1/databases/%s" % db_id)
    if not data:
        return {}
    schema = {}
    for name, prop in data.get("properties", {}).items():
        schema[name] = prop.get("type", "")
    return schema


READONLY_PROP_TYPES = frozenset([
    "formula", "rollup", "relation", "created_time", "last_edited_time",
    "created_by", "last_edited_by", "unique_id", "verification",
])


def build_property_payload(prop_name, text_value, prop_type):
    """Convert a text value back to Notion property format. Returns None for unsupported types."""
    if prop_type in READONLY_PROP_TYPES:
        return None
    if not text_value and prop_type not in ("checkbox",):
        return None

    if prop_type == "title":
        return {"title": [{"text": {"content": text_value}}]}
    elif prop_type == "rich_text":
        return {"rich_text": [{"text": {"content": text_value}}]}
    elif prop_type == "number":
        try:
            return {"number": float(text_value)}
        except (ValueError, TypeError):
            return None
    elif prop_type == "select":
        return {"select": {"name": text_value}}
    elif prop_type == "multi_select":
        names = [n.strip() for n in text_value.split(",") if n.strip()]
        return {"multi_select": [{"name": n} for n in names]}
    elif prop_type == "date":
        return {"date": {"start": text_value}}
    elif prop_type == "checkbox":
        return {"checkbox": text_value in ("True", "true", "1", True)}
    elif prop_type == "url":
        return {"url": text_value or None}
    elif prop_type == "status":
        return {"status": {"name": text_value}}
    elif prop_type == "email":
        return {"email": text_value or None}
    elif prop_type == "phone_number":
        return {"phone_number": text_value or None}
    return None


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


def _file_content_hash(filepath):
    """Compute SHA256 hash of file content. Returns hex string or empty on error."""
    try:
        h = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


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


def _resolve_child_links(md_text, parent_item, tree):
    """Replace [[📄 Title]] wikilinks with [📄 Title](./relative/path) Markdown links."""
    parent_filepath = item_to_filepath(parent_item)
    if not parent_filepath:
        return md_text
    parent_dir = parent_filepath.parent
    parent_path = parent_item.get("path", "")

    # Build lookup for direct children by matching tree paths.
    # Root page children have paths like "ChildTitle" (no parent prefix),
    # non-root children have paths like "Parent/ChildTitle".
    child_lookup = {}
    prefix = parent_path + "/"
    for t_item in tree:
        if t_item["id"] == parent_item.get("id"):
            continue
        t_path = t_item.get("path", "")
        is_direct_child = False
        if parent_item.get("is_root"):
            # Root's children: path has no "/" (top-level)
            if "/" not in t_path:
                is_direct_child = True
        else:
            if t_path.startswith(prefix):
                remainder = t_path[len(prefix):]
                if "/" not in remainder:
                    is_direct_child = True

        if is_direct_child:
            child_lookup[(t_item["type"], t_item["title"])] = t_item

    def _replace(match):
        icon = match.group(1)
        title = match.group(2)
        child_type = "page" if icon == "\U0001f4c4" else "db"

        child_item = child_lookup.get((child_type, title))
        if not child_item:
            return match.group(0)

        if child_type == "page":
            child_fp = item_to_filepath(child_item)
        else:
            child_fp = db_filepath(child_item["title"], child_item.get("path", ""))

        if not child_fp:
            return match.group(0)

        try:
            rel_path = os.path.relpath(child_fp, parent_dir)
        except ValueError:
            return match.group(0)

        if " " in rel_path or "(" in rel_path or ")" in rel_path:
            return "[%s %s](<%s>)" % (icon, title, rel_path)
        return "[%s %s](%s)" % (icon, title, rel_path)

    return re.sub('\[\[(\U0001f4c4|\U0001f5c3\ufe0f)\s+(.+?)\]\]', _replace, md_text)


def download_item(item, tree=None):
    if item["type"] == "page":
        filepath = item_to_filepath(item)
        if not filepath:
            return False
        filepath.parent.mkdir(parents=True, exist_ok=True)

        md = fetch_page_blocks_as_text(item["id"], page_dir=filepath.parent)
        if tree:
            md = _resolve_child_links(md, item, tree)
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


_WIKILINK_RE = re.compile('^\[\[(\U0001f4c4|\U0001f5c3\ufe0f)\s+(.+)\]\]$')
_CHILD_LINK_RE = re.compile('^\[(\U0001f4c4|\U0001f5c3\ufe0f)\s+(.+?)\]\(<?(.+?)>?\)$')
_MEDIA_LINK_RE = re.compile(r'^\[(📎|🎬|🔊|📁)\s+(.+?)\]\((.+?)\)$')


def markdown_to_notion_blocks(md_text):
    """Convert Markdown to Notion blocks. Child references (wikilink or relative link)
    are returned as placeholder dicts with type '_wikilink' so callers can handle
    child block positioning."""
    blocks = []
    lines = md_text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]

        wl = _WIKILINK_RE.match(line.strip())
        cl = _CHILD_LINK_RE.match(line.strip()) if not wl else None
        if wl or cl:
            m = wl or cl
            icon, title = m.group(1), m.group(2)
            child_type = "child_page" if icon == "\U0001f4c4" else "child_database"
            blocks.append({"_wikilink": True, "type": child_type, "title": title})
            i += 1
            continue

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
        elif line.startswith("###### "):
            # H6 → styled paragraph (▹ bold)
            htext = line[7:]
            blocks.append({"object": "block", "type": "paragraph",
                "paragraph": {"rich_text": [
                    {"type": "text", "text": {"content": "▹ "}},
                    {"type": "text", "text": {"content": htext}, "annotations": {"bold": True}},
                ]}})
        elif line.startswith("##### "):
            # H5 → styled paragraph (▸ bold)
            htext = line[6:]
            blocks.append({"object": "block", "type": "paragraph",
                "paragraph": {"rich_text": [
                    {"type": "text", "text": {"content": "▸ "}},
                    {"type": "text", "text": {"content": htext}, "annotations": {"bold": True}},
                ]}})
        elif line.startswith("#### "):
            blocks.append({"object": "block", "type": "heading_4",
                "heading_4": {"rich_text": parse_inline_markdown(line[5:])}})
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
            ml = _MEDIA_LINK_RE.match(line.strip()) if not alt else None
            if img_url:
                is_ext = img_url.startswith("http://") or img_url.startswith("https://")
                if is_ext:
                    blocks.append({"object": "block", "type": "image",
                        "image": {"type": "external", "external": {"url": img_url},
                                  "caption": parse_inline_markdown(alt) if alt else []}})
                else:
                    blocks.append({"_local_upload": True, "type": "image",
                        "local_path": img_url,
                        "caption_rt": parse_inline_markdown(alt) if alt else []})
            elif ml:
                icon, caption, url = ml.group(1), ml.group(2), ml.group(3)
                _ICON_TO_BTYPE = {"📎": "pdf", "🎬": "video", "🔊": "audio", "📁": "file"}
                media_type = _ICON_TO_BTYPE.get(icon, "file")
                is_external = url.startswith("http://") or url.startswith("https://")
                if is_external:
                    blocks.append({"object": "block", "type": media_type,
                        media_type: {"type": "external", "external": {"url": url},
                                     "caption": parse_inline_markdown(caption) if caption else []}})
                else:
                    blocks.append({"_local_upload": True, "type": media_type,
                        "local_path": url,
                        "caption_rt": parse_inline_markdown(caption) if caption else []})
            elif line.strip():
                blocks.append({"object": "block", "type": "paragraph",
                    "paragraph": {"rich_text": parse_inline_markdown(line.strip())}})
        i += 1

    return blocks


def _cmd_pull_db(db_path, dry_run=False):
    """Pull (re-fetch) a single .db file from Notion."""
    meta = _read_db_metadata(db_path)
    db_id = meta.get("notion_db_id", "")
    db_title = meta.get("db_title", db_path.stem)
    db_notion_path = meta.get("notion_db_path", "")

    if not db_id:
        print("ERROR: No notion_db_id in _metadata of %s" % db_path, flush=True)
        return False

    if dry_run:
        print("=== DB Pull DRY RUN ===", flush=True)
        print("File: %s" % db_path, flush=True)
        print("Notion DB ID: %s" % db_id, flush=True)
        print("Title: %s" % db_title, flush=True)

        payload = {"page_size": 1}
        data = api_post("https://api.notion.com/v1/databases/%s/query" % db_id, payload)
        if data:
            # Notion doesn't return total count directly; estimate via has_more
            print("Querying Notion for row count...", flush=True)
            total = 0
            cursor = None
            while True:
                p = {"page_size": 100}
                if cursor:
                    p["start_cursor"] = cursor
                d = api_post("https://api.notion.com/v1/databases/%s/query" % db_id, p)
                if not d:
                    break
                total += len(d.get("results", []))
                if not d.get("has_more"):
                    break
                cursor = d.get("next_cursor")
                time.sleep(CFG.rate_limit_delay)
            print("Notion rows: %d" % total, flush=True)

        schema = fetch_db_schema(db_id)
        print("Properties: %s" % ", ".join(sorted(schema.keys())), flush=True)
        print("db_page_content: %s" % CFG.db_page_content, flush=True)
        print("\n(dry-run: local file not modified)", flush=True)
        return True

    print("=== DB Pull from Notion ===", flush=True)
    print("File: %s" % db_path, flush=True)
    print("Notion DB ID: %s" % db_id, flush=True)

    count = fetch_database_to_sqlite(db_id, db_title, db_notion_path)
    print("Downloaded: %d rows" % count, flush=True)
    print("DB Pull complete.", flush=True)
    return True


def cmd_pull(filepath, dry_run=False):
    p = Path(filepath)
    if not p.exists():
        print("ERROR: File not found: %s" % filepath, flush=True)
        return False

    if p.suffix == ".db":
        return _cmd_pull_db(p, dry_run)

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

    md = fetch_page_blocks_as_text(notion_id, page_dir=p.parent)

    # Resolve child links if tree cache available
    if CFG.tree_json.exists() and notion_path:
        try:
            with open(CFG.tree_json) as f:
                tree = json.load(f)
            _mark_containers(tree)
            parent_item = None
            for t in tree:
                if t["id"] == notion_id:
                    parent_item = t
                    break
            if not parent_item:
                parent_item = {"id": notion_id, "path": notion_path, "type": "page",
                               "title": notion_path.split("/")[-1]}
            md = _resolve_child_links(md, parent_item, tree)
        except Exception:
            pass

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


def _cmd_push_db(db_path, dry_run=False):
    """Push a .db file to Notion (update existing rows + create new rows)."""
    meta = _read_db_metadata(db_path)
    db_id = meta.get("notion_db_id", "")
    db_title = meta.get("db_title", db_path.stem)

    if not db_id:
        print("ERROR: No notion_db_id in _metadata of %s" % db_path, flush=True)
        return False

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    try:
        c.execute("SELECT * FROM data")
        rows = c.fetchall()
        columns = [desc[0] for desc in c.description]
    except sqlite3.Error as e:
        print("ERROR: %s" % e, flush=True)
        conn.close()
        return False
    conn.close()

    schema = fetch_db_schema(db_id)
    if not schema:
        print("ERROR: Could not fetch schema for DB %s" % db_id, flush=True)
        return False

    skip_cols = {"_notion_page_id", "_created_time", "_last_edited_time", "_body"}
    writable_cols = [c for c in columns if c not in skip_cols and c in schema
                     and schema[c] not in READONLY_PROP_TYPES]

    updates = []
    creates = []
    for row in rows:
        page_id = row["_notion_page_id"] if "_notion_page_id" in columns else ""
        props = {}
        for col in writable_cols:
            val = row[col] or ""
            payload = build_property_payload(col, val, schema[col])
            if payload is not None:
                props[col] = payload
        if page_id:
            updates.append((page_id, props, row))
        else:
            creates.append((props, row))

    if dry_run:
        print("=== DB Push DRY RUN ===", flush=True)
        print("File: %s" % db_path, flush=True)
        print("Notion DB: %s (%s)" % (db_title, db_id), flush=True)
        print("Schema: %s" % ", ".join("%s(%s)" % (k, v) for k, v in sorted(schema.items())
                                        if v not in READONLY_PROP_TYPES), flush=True)
        print("", flush=True)
        print("Updates: %d rows (existing)" % len(updates), flush=True)
        for pid, props, row in updates[:5]:
            title_col = next((k for k, v in schema.items() if v == "title"), None)
            title = row[title_col] if title_col and title_col in columns else pid[:12]
            changed = [k for k in props]
            print("  [UPD] %s: %s" % (title, ", ".join(changed[:5])), flush=True)
        if len(updates) > 5:
            print("  ... and %d more" % (len(updates) - 5), flush=True)

        print("Creates: %d rows (new)" % len(creates), flush=True)
        for props, row in creates[:5]:
            title_col = next((k for k, v in schema.items() if v == "title"), None)
            title = ""
            if title_col and title_col in columns:
                title = row[title_col] or "(untitled)"
            print("  [NEW] %s" % title, flush=True)
        if len(creates) > 5:
            print("  ... and %d more" % (len(creates) - 5), flush=True)

        print("\n(dry-run: no changes written to Notion)", flush=True)
        return True

    print("=== DB Push to Notion ===", flush=True)
    print("File: %s" % db_path, flush=True)
    print("Notion DB: %s" % db_title, flush=True)

    success = 0
    errors = 0

    if updates:
        print("Updating %d rows..." % len(updates), flush=True)
        for i, (pid, props, row) in enumerate(updates):
            if not props:
                continue
            result = api_patch("https://api.notion.com/v1/pages/%s" % pid, {"properties": props})
            if result:
                success += 1
            else:
                errors += 1
            time.sleep(CFG.rate_limit_delay)
            if (i + 1) % 20 == 0:
                print("  ... updated %d/%d" % (i + 1, len(updates)), flush=True)

    if creates:
        print("Creating %d new rows..." % len(creates), flush=True)
        for i, (props, row) in enumerate(creates):
            body = {
                "parent": {"database_id": db_id},
                "properties": props,
            }
            result = api_post("https://api.notion.com/v1/pages", body)
            if result:
                success += 1
            else:
                errors += 1
            time.sleep(CFG.rate_limit_delay)
            if (i + 1) % 20 == 0:
                print("  ... created %d/%d" % (i + 1, len(creates)), flush=True)

    print("Push complete. Success: %d, Errors: %d" % (success, errors), flush=True)
    return errors == 0


def cmd_pull_recursive(notion_url, dry_run=False):
    """Recursively pull a subtree from Notion by URL or page ID."""
    page_id = extract_page_id_from_url(notion_url) if "notion.so" in notion_url else notion_url
    if not page_id:
        print("ERROR: Could not extract page ID from: %s" % notion_url, flush=True)
        return False

    title = _fetch_page_title(page_id)
    if not title:
        print("ERROR: Could not fetch page title for %s (check token/permissions)" % page_id, flush=True)
        return False

    print("=== Recursive Pull [%s] ===" % title, flush=True)
    print("Page ID: %s" % page_id, flush=True)

    parent_path = ""
    if CFG.tree_json.exists():
        with open(CFG.tree_json) as f:
            existing_tree = json.load(f)
        for item in existing_tree:
            if item["id"] == page_id:
                parent_path = item.get("path", "")
                break

    if not parent_path:
        parent_path = title

    print("Crawling subtree...", flush=True)
    sub_items = deep_crawl(page_id, parent_path, depth=0)

    root_item = {
        "type": "page", "title": title, "path": parent_path,
        "id": page_id, "depth": 0, "has_children": True, "is_root": False
    }
    all_items = [root_item] + sub_items

    pages = [i for i in all_items if i["type"] == "page"]
    dbs = [i for i in all_items if i["type"] == "db"]
    print("Found: %d pages, %d databases" % (len(pages), len(dbs)), flush=True)

    if CFG.tree_json.exists():
        with open(CFG.tree_json) as f:
            existing_tree = json.load(f)
        existing_ids = {i["id"] for i in existing_tree}
        new_count = 0
        for item in all_items:
            if item["id"] not in existing_ids:
                existing_tree.append(item)
                new_count += 1
            else:
                for i, ex in enumerate(existing_tree):
                    if ex["id"] == item["id"]:
                        existing_tree[i] = item
                        break
        with open(CFG.tree_json, "w") as f:
            json.dump(existing_tree, f, ensure_ascii=False, indent=1)
        print("Tree cache updated (+%d new, %d total)" % (new_count, len(existing_tree)), flush=True)
    else:
        with open(CFG.tree_json, "w") as f:
            json.dump(all_items, f, ensure_ascii=False, indent=1)
        print("Tree cache created (%d items)" % len(all_items), flush=True)

    if dry_run:
        print("\nWould download %d items:" % len(all_items), flush=True)
        for item in all_items[:20]:
            print("  [%s] %s" % (item["type"], item["title"][:60]), flush=True)
        if len(all_items) > 20:
            print("  ... and %d more" % (len(all_items) - 20), flush=True)
        print("\n(dry-run: no files downloaded)", flush=True)
        return True

    # Load full tree for link resolution
    full_tree = all_items
    if CFG.tree_json.exists():
        with open(CFG.tree_json) as f:
            full_tree = json.load(f)
    _mark_containers(full_tree)

    state = load_sync_state()
    success = 0
    errors = 0
    print("\nDownloading %d items..." % len(all_items), flush=True)
    for idx, item in enumerate(all_items):
        try:
            ok = download_item(item, tree=full_tree)
            if ok:
                success += 1
                entry = {
                    "type": item["type"],
                    "title": item["title"],
                    "path": item["path"],
                    "last_edited_time": get_page_last_edited(item["id"]) if item["type"] == "page" else "",
                    "synced_at": datetime.now().isoformat()
                }
                fp = item_to_filepath(item)
                if fp and fp.exists():
                    entry["content_hash"] = _file_content_hash(fp)
                state["items"][item["id"]] = entry
            print("  [%d/%d] %s: %s" % (
                idx + 1, len(all_items),
                "OK" if ok else "SKIP",
                item["title"][:50]
            ), flush=True)
        except Exception as e:
            errors += 1
            print("  [%d/%d] ERROR: %s (%s)" % (idx + 1, len(all_items), item["title"][:40], e), flush=True)
        time.sleep(CFG.rate_limit_delay)

    state["last_sync"] = datetime.now().isoformat()
    save_sync_state(state)

    print("\n=== Recursive Pull Complete ===", flush=True)
    print("Success: %d, Errors: %d" % (success, errors), flush=True)
    return errors == 0


def _match_wikilink_to_child(wl_block, child_blocks):
    """Match a wikilink placeholder to an existing child block by title and type."""
    wl_title = wl_block["title"].strip()
    wl_type = wl_block["type"]
    for cb in child_blocks:
        cb_type = cb["type"]
        cb_title = cb.get(cb_type, {}).get("title", "").strip()
        if cb_type == wl_type and cb_title == wl_title:
            return cb
    return None


def _split_blocks_by_wikilinks(new_blocks, child_blocks):
    """Split block list into segments around wikilink placeholders.

    Returns list of (segment_blocks, child_block_or_None) tuples.
    Each segment is content that goes BEFORE the paired child block.
    The last segment has child_block=None (content after all children).
    """
    segments = []
    current_segment = []

    for blk in new_blocks:
        if blk.get("_wikilink"):
            matched = _match_wikilink_to_child(blk, child_blocks)
            segments.append((current_segment, matched))
            current_segment = []
        else:
            current_segment.append(blk)

    segments.append((current_segment, None))
    return segments


# ==========================================
# New Page Creation & Scaffold
# ==========================================

_CHILD_LINK_RE_PARSE = re.compile(r'^\[(\U0001f4c4|\U0001f5c3\ufe0f)\s+(.+?)\]\(<?(.+?)>?\)$', re.MULTILINE)

def cmd_new(parent_ref, title, children=None):
    """Scaffold a new page structure with front matter and child links."""
    children = children or []

    parent_id = ""
    if parent_ref.startswith("http"):
        parent_id = extract_page_id_from_url(parent_ref)
    else:
        # Try to find parent in tree_cache by path or title
        tree = _load_tree_cache()
        for t_item in tree:
            if t_item.get("title") == parent_ref or t_item.get("path") == parent_ref:
                parent_id = t_item["id"]
                break
        if not parent_id:
            # Try as notion_id from a local .md file
            p = Path(parent_ref)
            if p.exists() and p.suffix == ".md":
                text = p.read_text(encoding="utf-8")
                fm, _ = parse_front_matter(text)
                parent_id = fm.get("notion_id", "")

    if not parent_id:
        print("ERROR: Could not resolve parent: %s" % parent_ref, flush=True)
        print("  Use a Notion URL, page title, tree path, or local .md file.", flush=True)
        return False

    safe_title = sanitize_filename(title)

    if children:
        # Container page → create directory
        page_dir = CFG.base_output_dir / safe_title
        page_dir.mkdir(parents=True, exist_ok=True)
        page_path = page_dir / (safe_title + ".md")
    else:
        page_path = CFG.base_output_dir / (safe_title + ".md")

    if page_path.exists():
        print("WARNING: File already exists: %s" % page_path, flush=True)
        print("  Use 'push' to update or delete the file first.", flush=True)
        return False

    # Build markdown content
    fm_lines = [
        "---",
        "notion_parent: %s" % parent_id,
        "title: %s" % title,
        "---",
    ]

    body_lines = []
    if children:
        body_lines.append("")
        for child_name in children:
            safe_child = sanitize_filename(child_name)
            child_dir = CFG.base_output_dir / safe_title / safe_child
            child_dir.mkdir(parents=True, exist_ok=True)
            child_path = child_dir / (safe_child + ".md")

            # Create child .md
            child_fm = [
                "---",
                "title: %s" % child_name,
                "---",
            ]
            child_path.write_text("\n".join(child_fm) + "\n\n", encoding="utf-8")
            print("  Created: %s" % child_path, flush=True)

            # Add relative link in parent
            rel = os.path.relpath(child_path, page_path.parent)
            if " " in rel or "(" in rel or ")" in rel:
                body_lines.append("[📄 %s](<%s>)" % (child_name, rel))
            else:
                body_lines.append("[📄 %s](%s)" % (child_name, rel))

    page_path.write_text("\n".join(fm_lines) + "\n" + "\n".join(body_lines) + "\n", encoding="utf-8")
    print("Created: %s" % page_path, flush=True)
    print("", flush=True)
    print("Next steps:", flush=True)
    print("  1. Edit the .md files with your content", flush=True)
    print("  2. Push to Notion:", flush=True)
    if children:
        print("     nsync push --recursive %s" % page_path, flush=True)
    else:
        print("     nsync push %s" % page_path, flush=True)
    return True


def _load_tree_cache():
    """Load tree_cache.json and return items list."""
    tc_path = CFG.sync_dir / "tree_cache.json"
    if tc_path.exists():
        with open(tc_path) as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return data.get("items", [])
    return []


def _save_tree_cache_items(items):
    """Save items to tree_cache.json (list format)."""
    tc_path = CFG.sync_dir / "tree_cache.json"
    CFG.sync_dir.mkdir(parents=True, exist_ok=True)
    with open(tc_path, "w") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def _create_notion_page(parent_id, title, blocks=None):
    """Create a new Notion page under parent_id. Returns the new page ID or empty string."""
    body = {
        "parent": {"page_id": parent_id},
        "properties": {
            "title": {
                "title": [{"text": {"content": title}}]
            }
        }
    }
    if blocks:
        body["children"] = blocks[:100]

    result = api_post("https://api.notion.com/v1/pages", body)
    if not result:
        print("ERROR: Failed to create page '%s'" % title, flush=True)
        return ""

    new_id = result.get("id", "")
    print("  Created Notion page: %s (id: %s)" % (title, new_id[:8] + "..."), flush=True)

    # If more than 100 blocks, append the rest
    if blocks and len(blocks) > 100:
        for i in range(100, len(blocks), 100):
            chunk = blocks[i:i + 100]
            api_patch("https://api.notion.com/v1/blocks/%s/children" % new_id, {"children": chunk})
            time.sleep(CFG.rate_limit_delay)

    time.sleep(CFG.rate_limit_delay)
    return new_id


def _find_children_in_md(md_text, page_dir):
    """Parse child link references from markdown. Returns [(title, type, filepath), ...]."""
    children = []
    for match in _CHILD_LINK_RE_PARSE.finditer(md_text):
        icon = match.group(1)
        title = match.group(2)
        rel_path = match.group(3)
        child_type = "page" if icon == "📄" else "db"
        child_fp = (page_dir / rel_path).resolve()
        children.append((title, child_type, child_fp))
    return children


def _validate_push_structure(filepath, recursive=False):
    """Validate structure before push. Returns (errors, warnings)."""
    errors = []
    warnings = []
    p = Path(filepath)

    if not p.exists():
        errors.append("File not found: %s" % filepath)
        return errors, warnings

    text = p.read_text(encoding="utf-8")
    fm, body = parse_front_matter(text)

    notion_id = fm.get("notion_id", "")
    notion_parent = fm.get("notion_parent", "")
    title = fm.get("title", "")

    if not notion_id and not notion_parent:
        errors.append("No notion_id or notion_parent in front matter of %s" % p.name)
    if not notion_id and not title:
        errors.append("New page needs 'title' in front matter: %s" % p.name)

    # Check child links
    children = _find_children_in_md(body, p.parent)
    for child_title, child_type, child_fp in children:
        if not child_fp.exists():
            errors.append("Child link target not found: %s (referenced in %s)" % (child_fp, p.name))
        elif child_type == "page" and recursive:
            child_text = child_fp.read_text(encoding="utf-8")
            child_fm, _ = parse_front_matter(child_text)
            if not child_fm.get("notion_id") and not child_fm.get("title"):
                errors.append("Child page needs 'title' in front matter: %s" % child_fp.name)

    # Check for duplicate names on Notion (if parent is known)
    if not notion_id and notion_parent and title:
        tree = _load_tree_cache()
        for t_item in tree:
            if t_item.get("title") == title:
                warnings.append("DUPLICATE: A page named '%s' already exists on Notion (id: %s). Will be skipped." % (title, t_item["id"][:8]))

    return errors, warnings


def cmd_push_new(filepath, dry_run=False, recursive=False):
    """Push a new local page (no notion_id) to Notion, creating it under notion_parent."""
    p = Path(filepath)
    text = p.read_text(encoding="utf-8")
    fm, body = parse_front_matter(text)

    title = fm.get("title", p.stem)
    notion_parent = fm.get("notion_parent", "")

    if not notion_parent:
        print("ERROR: No notion_parent in front matter. Cannot create page.", flush=True)
        return False

    # Check for duplicate on Notion
    tree = _load_tree_cache()
    for t_item in tree:
        if t_item.get("title") == title:
            # Check if this is a direct child of the parent
            parent_path = ""
            for pt in tree:
                if pt["id"] == notion_parent:
                    parent_path = pt.get("path", "")
                    break
            t_path = t_item.get("path", "")
            expected_prefix = parent_path + "/" if parent_path else ""
            if t_path == expected_prefix + title or (not parent_path and t_path == title):
                print("WARNING: Page '%s' already exists on Notion (id: %s). Skipping." % (title, t_item["id"][:8]), flush=True)
                # Write back the existing notion_id so future pushes work
                fm["notion_id"] = t_item["id"]
                fm_lines = ["---"]
                for k, v in fm.items():
                    if k != "notion_parent":
                        fm_lines.append("%s: %s" % (k, v))
                fm_lines.append("---")
                p.write_text("\n".join(fm_lines) + "\n\n" + body, encoding="utf-8")
                print("  Linked existing page. Use 'push %s' to update content." % filepath, flush=True)
                return True

    # Parse child links so we only send content blocks (not wikilinks)
    new_blocks = markdown_to_notion_blocks(body)
    content_blocks = [b for b in new_blocks if not b.get("_wikilink")]

    if dry_run:
        print("=== Push New Page DRY RUN ===", flush=True)
        print("File: %s" % filepath, flush=True)
        print("Title: %s" % title, flush=True)
        print("Parent: %s" % notion_parent, flush=True)
        print("Content blocks: %d" % len(content_blocks), flush=True)

        children = _find_children_in_md(body, p.parent)
        if children:
            print("Child references: %d" % len(children), flush=True)
            for ct, ctype, cfp in children:
                exists = "exists" if cfp.exists() else "MISSING"
                print("  %s %s (%s)" % ("📄" if ctype == "page" else "🗃️", ct, exists), flush=True)
        print("\n(dry-run: no changes written to Notion)", flush=True)
        return True

    print("=== Creating New Page on Notion ===", flush=True)
    print("Title: %s" % title, flush=True)
    print("Parent: %s" % notion_parent, flush=True)

    new_id = _create_notion_page(notion_parent, title, content_blocks)
    if not new_id:
        return False

    # Update front matter: remove notion_parent, add notion_id
    now = datetime.now().isoformat()
    new_fm = {"notion_id": new_id, "title": title, "synced_at": now, "pushed_at": now}
    fm_lines = ["---"]
    for k, v in new_fm.items():
        fm_lines.append("%s: %s" % (k, v))
    fm_lines.append("---")
    p.write_text("\n".join(fm_lines) + "\n\n" + body, encoding="utf-8")

    # Update sync_state
    state = load_sync_state()
    state.setdefault("items", {})[new_id] = {
        "filepath": str(p),
        "synced_at": now,
        "pushed_at": now,
        "content_hash": _file_content_hash(p),
    }
    save_sync_state(state)

    # Update tree_cache
    parent_item = None
    for t in tree:
        if t["id"] == notion_parent:
            parent_item = t
            break

    parent_path = parent_item["path"] if parent_item else ""
    new_tree_path = parent_path + "/" + title if parent_path else title
    new_tree_item = {
        "type": "page",
        "title": title,
        "path": new_tree_path,
        "id": new_id,
        "depth": (parent_item.get("depth", -1) + 1) if parent_item else 0,
        "has_children": False,
    }
    tree.append(new_tree_item)
    _save_tree_cache_items(tree)

    print("Page created successfully.", flush=True)

    # Recursive: create child pages
    if recursive:
        children = _find_children_in_md(body, p.parent)
        if children:
            print("Processing %d child references..." % len(children), flush=True)
            for child_title, child_type, child_fp in children:
                if child_type != "page":
                    print("  Skipping DB reference: %s" % child_title, flush=True)
                    continue
                if not child_fp.exists():
                    print("  WARNING: Child file not found: %s" % child_fp, flush=True)
                    continue

                child_text = child_fp.read_text(encoding="utf-8")
                child_fm, child_body = parse_front_matter(child_text)

                if child_fm.get("notion_id"):
                    print("  Skipping (already has notion_id): %s" % child_title, flush=True)
                    continue

                # Set notion_parent to the newly created page
                child_fm["notion_parent"] = new_id
                if not child_fm.get("title"):
                    child_fm["title"] = child_title
                child_fm_lines = ["---"]
                for k, v in child_fm.items():
                    child_fm_lines.append("%s: %s" % (k, v))
                child_fm_lines.append("---")
                child_fp.write_text("\n".join(child_fm_lines) + "\n\n" + child_body, encoding="utf-8")

                # Recursively push the child
                cmd_push_new(str(child_fp), dry_run=False, recursive=True)

        # Mark this page as container in tree
        new_tree_item["has_children"] = True
        _save_tree_cache_items(tree)

    return True


def _infer_parent_from_path(filepath):
    """Infer Notion parent page from local file path using tree_cache.

    Walks up the directory hierarchy to find a directory that corresponds to
    a known Notion page. Returns (parent_notion_id, title) or None.
    """
    p = Path(filepath).resolve()
    base = CFG.base_output_dir.resolve()

    try:
        p.relative_to(base)
    except ValueError:
        return None

    title = p.stem

    tree = _load_tree_cache()
    if not tree:
        return None

    # Find root item to determine root prefix in tree paths
    root_item = None
    for item in tree:
        if item.get("is_root"):
            root_item = item
            break

    parent_dir = p.parent
    try:
        rel_dir = parent_dir.relative_to(base)
    except ValueError:
        return None

    # If the file is directly in base dir, parent is root
    if str(rel_dir) == ".":
        if root_item:
            return (root_item["id"], title)
        return None

    dir_parts = list(rel_dir.parts)

    # Tree paths may include the root page name as prefix (e.g., "Palma/子ページ").
    # Local paths don't include the root name (base dir IS the root).
    # So we need to compare with and without root prefix.
    root_prefix = sanitize_filename(root_item["title"]) if root_item else ""

    # Strategy: match deepest directory first, walk up
    for depth in range(len(dir_parts), 0, -1):
        candidate_parts = dir_parts[:depth]

        for item in tree:
            if item["type"] != "page":
                continue
            item_path_parts = item["path"].split("/")
            item_sanitized = [sanitize_filename(x) for x in item_path_parts]

            # Direct match (tree paths without root prefix)
            if len(item_sanitized) == depth and item_sanitized == candidate_parts:
                return (item["id"], title)

            # Match with root prefix stripped (tree path = "Root/A/B", local = "A/B")
            if (len(item_sanitized) == depth + 1
                    and item_sanitized[0] == root_prefix
                    and item_sanitized[1:] == candidate_parts):
                return (item["id"], title)

    # Fallback: root page
    if root_item:
        return (root_item["id"], title)

    return None


def _append_blocks(page_id, blocks, after_id=None):
    """Append blocks to a page, optionally after a specific block. Returns last new block ID."""
    if not blocks:
        return after_id
    url = "https://api.notion.com/v1/blocks/%s/children" % page_id
    last_id = after_id
    for i in range(0, len(blocks), 100):
        chunk = blocks[i:i + 100]
        body = {"children": chunk}
        if last_id:
            body["after"] = last_id
        result = api_patch(url, body)
        if not result:
            print("  ERROR: Failed to append blocks (chunk %d)" % (i // 100), flush=True)
            return last_id
        new_results = result.get("results", [])
        if new_results:
            last_id = new_results[-1]["id"]
        time.sleep(CFG.rate_limit_delay)
    return last_id


_MEDIA_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".tiff", ".bmp", ".heic",
    ".pdf",
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp", ".wmv", ".flv",
    ".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a", ".wma",
}

_EXT_TO_BLOCK_TYPE = {
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
    ".webp": "image", ".svg": "image", ".tiff": "image", ".bmp": "image", ".heic": "image",
    ".pdf": "pdf",
    ".mp4": "video", ".mov": "video", ".avi": "video", ".mkv": "video", ".webm": "video",
    ".mp3": "audio", ".wav": "audio", ".ogg": "audio", ".flac": "audio", ".aac": "audio",
}

_BLOCK_TYPE_ICON = {"image": "", "pdf": "📎", "video": "🎬", "audio": "🔊", "file": "📁"}


def _find_standalone_media(page_dir, md_text):
    """Find media files in page_dir (and _assets/) not referenced in md_text.
    Returns list of Path objects."""
    standalone = []
    for f in sorted(page_dir.iterdir()):
        if f.is_dir() and f.name == "_assets":
            for af in sorted(f.iterdir()):
                if af.suffix.lower() in _MEDIA_EXTENSIONS:
                    rel = "_assets/" + af.name
                    if rel not in md_text and af.name not in md_text:
                        standalone.append(af)
        elif f.is_file() and f.suffix.lower() in _MEDIA_EXTENSIONS:
            if f.name not in md_text:
                standalone.append(f)
    return standalone


def _create_media_page_md(media_file, parent_notion_id, page_dir):
    """Create a .md file for a standalone media file. Returns the created .md Path."""
    stem = media_file.stem
    ext = media_file.suffix.lower()
    btype = _EXT_TO_BLOCK_TYPE.get(ext, "file")

    md_dir = page_dir / stem
    md_dir.mkdir(parents=True, exist_ok=True)
    md_path = md_dir / (stem + ".md")

    assets_dir = md_dir / "_assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    import shutil
    dest_asset = assets_dir / media_file.name
    if not dest_asset.exists():
        shutil.copy2(str(media_file), str(dest_asset))

    rel_asset = "_assets/" + media_file.name
    if btype == "image":
        content_line = "![%s](%s)" % (stem, rel_asset)
    else:
        icon = _BLOCK_TYPE_ICON.get(btype, "📁")
        content_line = "[%s %s](%s)" % (icon, stem, rel_asset)

    fm = "---\nnotion_parent: %s\ntitle: %s\n---\n\n%s\n" % (
        parent_notion_id, stem, content_line
    )
    md_path.write_text(fm, encoding="utf-8")
    return md_path


def _resolve_local_uploads(blocks, page_dir, dry_run=False):
    """Resolve _local_upload blocks by uploading files to Notion.
    Returns blocks with uploads replaced by proper Notion blocks."""
    resolved = []
    for blk in blocks:
        if not blk.get("_local_upload"):
            resolved.append(blk)
            continue
        local_path = page_dir / blk["local_path"]
        btype = blk["type"]
        caption_rt = blk.get("caption_rt", [])
        if not local_path.exists():
            print("  WARN: Local file not found, skipping: %s" % blk["local_path"], flush=True)
            continue
        if dry_run:
            resolved.append({"object": "block", "type": btype, "_local_upload_path": str(local_path),
                             btype: {"type": "external", "external": {"url": "file://" + str(local_path)},
                                     "caption": caption_rt}})
            continue
        file_upload_id = _upload_file_to_notion(local_path, purpose="block_content")
        if file_upload_id:
            resolved.append(_make_file_upload_block(file_upload_id, btype, caption_rt))
        else:
            print("  WARN: Upload failed, skipping: %s" % local_path.name, flush=True)
    return resolved


def cmd_push(filepath, dry_run=False, recursive=False):
    p = Path(filepath)
    if not p.exists():
        print("ERROR: File not found: %s" % filepath, flush=True)
        return False

    if p.suffix == ".db":
        return _cmd_push_db(p, dry_run)

    text = p.read_text(encoding="utf-8")
    fm, body = parse_front_matter(text)
    notion_id = fm.get("notion_id", "")

    if not notion_id:
        if not fm.get("notion_parent"):
            # Try to infer parent from directory structure
            inferred = _infer_parent_from_path(p)
            if inferred:
                parent_id, inferred_title = inferred
                fm["notion_parent"] = parent_id
                if not fm.get("title"):
                    fm["title"] = inferred_title
                # Write inferred front matter back so cmd_push_new can read it
                fm_lines = ["---"]
                for k, v in fm.items():
                    fm_lines.append("%s: %s" % (k, v))
                fm_lines.append("---")
                p.write_text("\n".join(fm_lines) + "\n\n" + body, encoding="utf-8")
                print("Inferred parent: %s (from directory structure)" % parent_id[:8], flush=True)
            elif not fm.get("title"):
                print("ERROR: Cannot determine where to create page.", flush=True)
                print("  Add 'notion_parent' to front matter, or place the file inside", flush=True)
                print("  an existing nsync workspace directory.", flush=True)
                return False
        if not fm.get("title"):
            fm["title"] = p.stem
        return cmd_push_new(filepath, dry_run=dry_run, recursive=recursive)

    new_blocks = markdown_to_notion_blocks(body)
    new_blocks = _resolve_local_uploads(new_blocks, p.parent, dry_run)
    has_wikilinks = any(blk.get("_wikilink") for blk in new_blocks)

    if dry_run:
        print("=== Push DRY RUN ===", flush=True)
        print("File: %s" % filepath, flush=True)
        print("Notion ID: %s" % notion_id, flush=True)
        content_blocks = [b for b in new_blocks if not b.get("_wikilink")]
        print("Blocks to upload: %d" % len(content_blocks), flush=True)
        if has_wikilinks:
            wl_count = sum(1 for b in new_blocks if b.get("_wikilink"))
            print("Child references (preserved): %d" % wl_count, flush=True)

        # Structure validation
        if recursive:
            errs, warns = _validate_push_structure(filepath, recursive=True)
            if warns:
                print("", flush=True)
                for w in warns:
                    print("  ⚠ %s" % w, flush=True)
            if errs:
                print("", flush=True)
                for e in errs:
                    print("  ✗ %s" % e, flush=True)
        print("", flush=True)
        for i, blk in enumerate(new_blocks):
            if blk.get("_wikilink"):
                icon = "📄" if blk["type"] == "child_page" else "🗃️"
                print("  [%d] %s: [[%s %s]]" % (i + 1, blk["type"], icon, blk["title"]), flush=True)
                continue
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

    existing = get_blocks(notion_id)
    child_blocks = [b for b in existing if b["type"] in ("child_page", "child_database")]
    non_child_blocks = [b for b in existing if b["type"] not in ("child_page", "child_database")]

    if has_wikilinks and child_blocks:
        print("Position-aware push (%d children detected)..." % len(child_blocks), flush=True)
        segments = _split_blocks_by_wikilinks(new_blocks, child_blocks)

        # Strategy: insert new content using existing blocks as anchors,
        # then delete old non-child blocks.
        #
        # For seg_0 (before first child): insert after the last OLD block
        # that precedes the first child, then delete old blocks later.
        # For seg_N: insert after child_N using `after` parameter.

        # Build a map: for each child block, find the OLD block just before it
        old_block_ids = set(b["id"] for b in non_child_blocks)
        anchor_before_first_child = None
        for b in existing:
            if b["type"] in ("child_page", "child_database"):
                break
            anchor_before_first_child = b["id"]

        # Pass 1: Insert content segments in order
        content_count = 0
        for seg_idx, (seg_blocks, paired_child) in enumerate(segments):
            if not seg_blocks:
                if paired_child:
                    continue
                continue

            if seg_idx == 0 and paired_child:
                # First segment (before first child)
                if anchor_before_first_child:
                    print("  Inserting segment 0 (%d blocks, before first child)..." % len(seg_blocks), flush=True)
                    _append_blocks(notion_id, seg_blocks, after_id=anchor_before_first_child)
                else:
                    # Edge case: child is the very first block, no anchor available.
                    # Append at end; will be after all children (unavoidable API limitation).
                    print("  Inserting segment 0 (%d blocks, appending)..." % len(seg_blocks), flush=True)
                    _append_blocks(notion_id, seg_blocks, after_id=None)
                content_count += len(seg_blocks)
            elif paired_child:
                # Content before this child → insert after the PREVIOUS child
                prev_child_id = None
                for prev_seg_idx in range(seg_idx - 1, -1, -1):
                    pc = segments[prev_seg_idx][1]
                    if pc:
                        prev_child_id = pc["id"]
                        break
                if prev_child_id:
                    print("  Inserting segment %d (%d blocks, after child)..." % (seg_idx, len(seg_blocks)), flush=True)
                    _append_blocks(notion_id, seg_blocks, after_id=prev_child_id)
                else:
                    _append_blocks(notion_id, seg_blocks, after_id=anchor_before_first_child)
                content_count += len(seg_blocks)
            else:
                # Last segment (after all children)
                last_child_id = None
                for s_idx in range(len(segments) - 2, -1, -1):
                    pc = segments[s_idx][1]
                    if pc:
                        last_child_id = pc["id"]
                        break
                if last_child_id:
                    print("  Inserting final segment (%d blocks, after last child)..." % len(seg_blocks), flush=True)
                    _append_blocks(notion_id, seg_blocks, after_id=last_child_id)
                else:
                    _append_blocks(notion_id, seg_blocks, after_id=None)
                content_count += len(seg_blocks)

        # Pass 2: Delete old non-child blocks
        print("  Removing %d old blocks..." % len(non_child_blocks), flush=True)
        for b in non_child_blocks:
            api_delete("https://api.notion.com/v1/blocks/%s" % b["id"])
            time.sleep(CFG.rate_limit_delay)

        print("  Uploaded %d content blocks, preserved %d children" % (content_count, len(child_blocks)), flush=True)

    else:
        # Simple push: no wikilinks or no children
        print("Removing existing blocks...", flush=True)
        for b in non_child_blocks:
            api_delete("https://api.notion.com/v1/blocks/%s" % b["id"])
            time.sleep(CFG.rate_limit_delay)

        content_blocks = [b for b in new_blocks if not b.get("_wikilink")]
        print("Uploading %d blocks..." % len(content_blocks), flush=True)
        url = "https://api.notion.com/v1/blocks/%s/children" % notion_id
        for i in range(0, len(content_blocks), 100):
            chunk = content_blocks[i:i + 100]
            result = api_patch(url, {"children": chunk})
            if not result:
                print("  ERROR: Failed to append blocks (chunk %d)" % (i // 100), flush=True)
                return False
            time.sleep(CFG.rate_limit_delay)
            print("  Uploaded blocks %d-%d" % (i + 1, min(i + 100, len(content_blocks))), flush=True)

    now = datetime.now().isoformat()
    fm["synced_at"] = now
    fm["pushed_at"] = now
    fm_lines = ["---"]
    for k, v in fm.items():
        fm_lines.append("%s: %s" % (k, v))
    fm_lines.append("---")
    p.write_text("\n".join(fm_lines) + "\n\n" + body, encoding="utf-8")

    # Update sync state with new content hash
    state = load_sync_state()
    if notion_id in state.get("items", {}):
        state["items"][notion_id]["synced_at"] = now
        state["items"][notion_id]["pushed_at"] = now
        state["items"][notion_id]["content_hash"] = _file_content_hash(p)
        save_sync_state(state)

    print("Push complete.", flush=True)

    # Recursive: process child page links that don't have notion_id yet
    if recursive:
        children = _find_children_in_md(body, p.parent)
        new_children = []
        for child_title, child_type, child_fp in children:
            if child_type != "page" or not child_fp.exists():
                continue
            child_text = child_fp.read_text(encoding="utf-8")
            child_fm, child_body = parse_front_matter(child_text)
            if child_fm.get("notion_id"):
                # Already exists → push update recursively
                cmd_push(str(child_fp), dry_run=False, recursive=True)
            else:
                # New child → set parent and create
                child_fm["notion_parent"] = notion_id
                if not child_fm.get("title"):
                    child_fm["title"] = child_title
                child_fm_lines = ["---"]
                for k, v in child_fm.items():
                    child_fm_lines.append("%s: %s" % (k, v))
                child_fm_lines.append("---")
                child_fp.write_text("\n".join(child_fm_lines) + "\n\n" + child_body, encoding="utf-8")
                cmd_push_new(str(child_fp), dry_run=False, recursive=True)
                new_children.append(child_title)

        if new_children:
            print("  Created %d child pages: %s" % (len(new_children), ", ".join(new_children)), flush=True)

    # Standalone media files: create pages and push
    standalone = _find_standalone_media(p.parent, body)
    if standalone:
        print("  Standalone media files: %d" % len(standalone), flush=True)
        for mf in standalone:
            print("  Creating page for: %s" % mf.name, flush=True)
            if dry_run:
                print("    (dry-run: would create page '%s')" % mf.stem, flush=True)
                continue
            md_path = _create_media_page_md(mf, notion_id, p.parent)
            cmd_push_new(str(md_path), dry_run=False, recursive=False)

            # Add child link to parent MD
            ext = mf.suffix.lower()
            btype = _EXT_TO_BLOCK_TYPE.get(ext, "file")
            if btype == "image":
                link_line = "[📄 %s](%s/%s.md)" % (mf.stem, mf.stem, mf.stem)
            else:
                link_line = "[📄 %s](%s/%s.md)" % (mf.stem, mf.stem, mf.stem)
            body_with_link = body.rstrip() + "\n" + link_line
            fm_lines = ["---"]
            for k, v in fm.items():
                fm_lines.append("%s: %s" % (k, v))
            fm_lines.append("---")
            p.write_text("\n".join(fm_lines) + "\n\n" + body_with_link, encoding="utf-8")
            body = body_with_link

            # Remove original standalone file (now moved to child page _assets/)
            if mf.exists() and (p.parent / mf.stem / "_assets" / mf.name).exists():
                mf.unlink()
                print("    Moved to %s/_assets/%s" % (mf.stem, mf.name), flush=True)

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


def _detect_local_changes(state):
    """Detect locally modified files using content hash comparison.

    Uses SHA256 hash stored at sync time. Falls back to mtime with a
    generous threshold (300s) for entries without a stored hash.
    Returns list of (item_id, filepath, state_entry) for files changed locally.
    """
    changed = []
    for item_id, entry in state.get("items", {}).items():
        synced_at = entry.get("synced_at", "")
        if not synced_at:
            continue
        item_type = entry.get("type", "page")
        if item_type != "page":
            continue

        filepath = None
        item_path = entry.get("path", "")
        if item_path:
            parts = item_path.split("/")
            sanitized = [sanitize_filename(p) for p in parts]
            fname = sanitized[-1]
            if not fname.endswith(".md"):
                fname += ".md"
            if len(sanitized) > 1:
                filepath = CFG.base_output_dir / os.path.join(*sanitized[:-1]) / fname
            else:
                filepath = CFG.base_output_dir / fname
            container_path = CFG.base_output_dir / os.path.join(*sanitized) / fname
            if container_path.exists():
                filepath = container_path

        if not filepath or not filepath.exists():
            continue

        stored_hash = entry.get("content_hash", "")
        if stored_hash:
            current_hash = _file_content_hash(filepath)
            if current_hash and current_hash != stored_hash:
                changed.append((item_id, filepath, entry))
        else:
            # Fallback for old entries without hash: mtime with 5min threshold
            try:
                mtime = os.path.getmtime(filepath)
                synced_ts = datetime.fromisoformat(synced_at).timestamp()
                if mtime - synced_ts > 300:
                    changed.append((item_id, filepath, entry))
            except (OSError, ValueError):
                continue
    return changed


def _auto_push_local_changes(local_changes, remote_changed_ids, dry_run=False):
    """Push locally modified files to Notion. Skip conflicts (both modified).

    Returns (pushed_count, conflict_ids).
    """
    pushed = 0
    conflicts = set()

    if not local_changes:
        return pushed, conflicts

    print("--- Local Changes Detected ---", flush=True)
    print("Found %d locally modified file(s)" % len(local_changes), flush=True)

    for item_id, filepath, entry in local_changes:
        title = entry.get("title", filepath.name)[:60]

        if item_id in remote_changed_ids:
            conflicts.add(item_id)
            print("  CONFLICT: %s (both local & remote changed, skipping)" % title, flush=True)
            continue

        if dry_run:
            print("  PUSH (dry): %s" % title, flush=True)
            continue

        print("  PUSH: %s" % title, flush=True)
        ok = cmd_push(str(filepath), dry_run=False)
        if ok:
            pushed += 1
        else:
            print("  WARN: Push failed for %s" % title, flush=True)

    print("Pushed: %d, Conflicts: %d" % (pushed, len(conflicts)), flush=True)
    if conflicts:
        print("(Conflicting files left untouched. Resolve manually, then push/pull.)", flush=True)
    print(flush=True)
    return pushed, conflicts


def _detect_renames(tree, state):
    """Detect pages renamed on Notion and move local files accordingly.
    Returns count of renames performed."""
    renames = 0
    _mark_containers(tree)
    for item in tree:
        item_id = item["id"]
        if item_id not in state.get("items", {}):
            continue
        old_path = state["items"][item_id].get("path", "")
        new_path = item.get("path", "")
        if not old_path or not new_path or old_path == new_path:
            continue

        old_item = dict(item)
        old_item["path"] = old_path
        old_filepath = item_to_filepath(old_item)
        new_filepath = item_to_filepath(item)

        if not old_filepath or not new_filepath:
            continue
        if not old_filepath.exists():
            continue
        if old_filepath == new_filepath:
            continue

        new_filepath.parent.mkdir(parents=True, exist_ok=True)
        import shutil

        if item.get("is_container") or item.get("is_root"):
            old_dir = old_filepath.parent
            new_dir = new_filepath.parent
            if old_dir.is_dir() and old_dir != new_dir:
                shutil.move(str(old_dir), str(new_dir))
                print("  Renamed dir: %s → %s" % (
                    old_dir.relative_to(CFG.base_output_dir),
                    new_dir.relative_to(CFG.base_output_dir)), flush=True)
                # The .md file inside also needs renaming
                old_md_in_new_dir = new_dir / old_filepath.name
                if old_md_in_new_dir.exists() and old_md_in_new_dir != new_filepath:
                    shutil.move(str(old_md_in_new_dir), str(new_filepath))
        else:
            shutil.move(str(old_filepath), str(new_filepath))
            print("  Renamed: %s → %s" % (
                old_filepath.relative_to(CFG.base_output_dir),
                new_filepath.relative_to(CFG.base_output_dir)), flush=True)

        # Update front matter notion_path in the moved file
        if new_filepath.exists() and new_filepath.suffix == ".md":
            try:
                txt = new_filepath.read_text(encoding="utf-8")
                if "notion_path:" in txt:
                    txt = re.sub(
                        r'(notion_path:\s*).*',
                        r'\g<1>' + new_path,
                        txt, count=1
                    )
                    new_filepath.write_text(txt, encoding="utf-8")
            except Exception:
                pass

        state["items"][item_id]["path"] = new_path
        state["items"][item_id]["title"] = item.get("title", "")
        if new_filepath.exists():
            state["items"][item_id]["content_hash"] = _file_content_hash(new_filepath)
        renames += 1

    return renames


def cmd_sync(force=False, dry_run=False, refresh=False, no_push=False):
    if refresh or not CFG.tree_json.exists():
        if not CFG.tree_json.exists():
            print("No tree cache found. Running refresh...", flush=True)
        tree = cmd_crawl()
    else:
        with open(CFG.tree_json) as f:
            tree = json.load(f)

    state = load_sync_state()
    now = datetime.now().isoformat()

    # Phase 0: Detect renames (Notion page title changes → move local files)
    rename_count = _detect_renames(tree, state)
    if rename_count > 0:
        print("Renames detected: %d" % rename_count, flush=True)
        save_sync_state(state)

    # Phase 1: Detect local changes (fast, local-only)
    local_changes = [] if no_push else _detect_local_changes(state)

    new_items = []
    updated_items = []
    unchanged = 0
    errors = 0

    print("=== Bidirectional Sync [%s] ===" % CFG.label, flush=True)
    print("Started: " + now, flush=True)
    print("Total items: %d" % len(tree), flush=True)
    if local_changes:
        print("Local modifications: %d" % len(local_changes), flush=True)
    print("Checking remote changes...", flush=True)
    print(flush=True)

    # Phase 2: Check remote changes
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

    # Phase 3: Push local changes before downloading remote
    remote_changed_ids = set(i["id"] for i in updated_items)
    if local_changes:
        pushed_count, conflict_ids = _auto_push_local_changes(
            local_changes, remote_changed_ids, dry_run=dry_run
        )
        # Remove conflict items from download list
        if conflict_ids:
            updated_items = [i for i in updated_items if i["id"] not in conflict_ids]
    else:
        conflict_ids = set()

    print("New: %d, Updated: %d, Unchanged: %d" % (len(new_items), len(updated_items), unchanged), flush=True)
    if conflict_ids:
        print("Conflicts (skipped): %d" % len(conflict_ids), flush=True)

    # Phase 4: Download remote changes
    to_download = new_items + updated_items
    if not to_download:
        print("Nothing to sync from Notion.", flush=True)
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

        ok = download_item(item, tree=tree)
        if ok:
            success += 1
        else:
            errors += 1

        remote_edited = ""
        if item["type"] == "page":
            remote_edited = get_page_last_edited(item["id"])
            time.sleep(CFG.rate_limit_delay)

        entry = {
            "title": item["title"], "path": item["path"],
            "type": item["type"], "last_edited_time": remote_edited,
            "synced_at": datetime.now().isoformat(),
        }
        fp = item_to_filepath(item)
        if fp and fp.exists():
            entry["content_hash"] = _file_content_hash(fp)
        state["items"][item["id"]] = entry
        save_sync_state(state)
        time.sleep(CFG.rate_limit_delay)

    state["last_sync"] = now
    save_sync_state(state)

    print(flush=True)
    print("=== Sync Complete ===", flush=True)
    print("Success: %d, Errors: %d (New: %d, Updated: %d)" % (
        success, errors, len(new_items), len(updated_items)), flush=True)
    if conflict_ids:
        print("Conflicts: %d (resolve manually)" % len(conflict_ids), flush=True)
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
            entry = {
                "title": item["title"], "path": item["path"],
                "type": item["type"], "last_edited_time": "",
                "synced_at": datetime.now().isoformat(),
            }
            if filepath and filepath.exists():
                entry["content_hash"] = _file_content_hash(filepath)
            state["items"][item["id"]] = entry
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

    hb = _heartbeat_path()
    cp = _load_checkpoint()
    if hb.exists():
        hb_time = hb.read_text().strip()
        age = (datetime.now() - datetime.fromisoformat(hb_time)).total_seconds()
        if age < 10:
            print("Crawl: RUNNING (heartbeat %ds ago)" % int(age), flush=True)
        else:
            print("Crawl: STALE heartbeat (%ds ago — may have crashed)" % int(age), flush=True)
        if cp:
            print("  Checkpoint: %d items, %d queue" % (len(cp["items"]), len(cp["queue"])), flush=True)
    elif cp:
        print("Crawl: STOPPED (checkpoint available: %d items, %d queue)" % (
            len(cp["items"]), len(cp["queue"])), flush=True)
        print("  Run 'sync' to resume from checkpoint", flush=True)

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
        no_push = "--no-push" in args
        if full:
            refresh = True
            force = True
        cmd_sync(force=force, dry_run=dry_run, refresh=refresh, no_push=no_push)
    elif command == "init-state":
        cmd_init_state()
    elif command == "status":
        cmd_status()
    elif command == "full":
        cmd_sync(force=True, refresh=True)
    elif command == "pull":
        dry_run = "--dry-run" in args
        recursive = "--recursive" in args or "-r" in args
        pull_args = [a for a in args[1:] if a not in ("--dry-run", "--recursive", "-r")]
        if len(pull_args) < 1:
            print("Usage: nsync.py pull [--dry-run] [--recursive|-r] <filepath.md|notion-url>", flush=True)
            sys.exit(1)
        if recursive:
            cmd_pull_recursive(pull_args[0], dry_run=dry_run)
        else:
            cmd_pull(pull_args[0], dry_run=dry_run)
    elif command == "new":
        new_args = [a for a in args[1:] if not a.startswith("--")]
        children_str = ""
        for i, a in enumerate(args):
            if a == "--children" and i + 1 < len(args):
                children_str = args[i + 1]
                break
        if len(new_args) < 2:
            print('Usage: nsync.py new <parent-url-or-title> "Page Title" [--children "Child1,Child2"]', flush=True)
            sys.exit(1)
        parent_ref = new_args[0]
        title = new_args[1]
        children = [c.strip() for c in children_str.split(",") if c.strip()] if children_str else []
        cmd_new(parent_ref, title, children)
    elif command == "push":
        dry_run = "--dry-run" in args
        recursive = "--recursive" in args or "-r" in args
        push_args = [a for a in args[1:] if a not in ("--dry-run", "--recursive", "-r")]
        if len(push_args) < 1:
            print('Usage: nsync.py push [--dry-run] [--recursive|-r] <filepath.md>', flush=True)
            sys.exit(1)
        cmd_push(push_args[0], dry_run=dry_run, recursive=recursive)
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
    print("  sync               Bidirectional sync (push local changes, pull remote)", flush=True)
    print("  sync --refresh     Refresh page list, then sync", flush=True)
    print("  sync --force       Force re-download all pages", flush=True)
    print("  sync --full         Refresh + force (complete re-sync)", flush=True)
    print("  sync --dry-run     Show what would be synced", flush=True)
    print("  sync --no-push     Skip local change detection & auto-push", flush=True)
    print("  pull <file>        Pull single file from Notion (.md or .db)", flush=True)
    print("  pull -r <url>      Recursively pull a subtree by Notion URL", flush=True)
    print("  push <file>        Push local file to Notion (.md or .db)", flush=True)
    print("  push -r <file>     Push recursively (create child pages too)", flush=True)
    print('  new <parent> "Title" [--children "A,B,C"]  Scaffold new page', flush=True)
    print("  status             Show sync status", flush=True)
    print("  init-state         Init state from existing files", flush=True)
    print("  db-list            List all databases", flush=True)
    print('  query <db> "SQL"   Query a database', flush=True)


def _print_api_stats():
    s = _api_stats
    if s["calls"] == 0:
        return
    parts = ["API: %d calls" % s["calls"]]
    if s["rate_limits"]:
        parts.append("%d rate-limited" % s["rate_limits"])
    if s["errors"]:
        parts.append("%d errors" % s["errors"])
    if s["skipped"]:
        parts.append("%d skipped" % s["skipped"])
    print("[%s]" % ", ".join(parts), flush=True)


if __name__ == "__main__":
    try:
        main()
    except RateLimitExhausted as e:
        print("\nFATAL: %s" % e, flush=True)
        print("Checkpoint saved. Re-run the same command to resume.", flush=True)
        sys.exit(2)
    finally:
        _print_api_stats()
