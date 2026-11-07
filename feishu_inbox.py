#!/usr/bin/env python3
"""从飞书群拉新消息，把链接追加进链接收件箱（可选功能，不用飞书可以完全不碰）。

不加第三方依赖，只用标准库 urllib 请求飞书 Open API。
凭证从 $RAYS_BRAIN_STATE/feishu.json 读，不存在回退 ~/.local/state/rays-brain/feishu.json。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

STATE_HOME = Path(os.environ.get("RAYS_BRAIN_STATE", str(Path.home() / ".local/state/rays-brain")))
DEFAULT_STATE_HOME = Path.home() / ".local/state/rays-brain"
TOKEN_FILE = STATE_HOME / "feishu-token.json"

URL_RE = re.compile(r"https?://[^\s<>\"'\]\)\\]+")

# Access token cache: refresh when remaining lifetime < 5 minutes
TOKEN_MARGIN_SECONDS = 300
# First run: only pull messages from the last 3 days
FIRST_RUN_LOOKBACK_DAYS = 3
# Overlap window to avoid missing messages between runs
OVERLAP_SECONDS = 60
# Maximum messages pulled per invocation (pagination safety cap)
MAX_MESSAGES_PER_PULL = 200
# Maximum seen message IDs to retain in state
MAX_SEEN_IDS = 500
# Maximum note text length appended per inbox line
MAX_NOTE_LENGTH = 300

def _不记录日志(_message: str) -> None:
    pass


class FeishuSkipped(Exception):
    """Raised when Feishu should be silently skipped (e.g. missing credentials)."""


# ---------------------------------------------------------- Credentials

def load_credentials(log: Callable[[str], None] = _不记录日志) -> dict[str, Any]:
    """Load Feishu app credentials from state directory, with fallback.

    Returns dict with app_id, app_secret, chat_name.
    Raises FeishuSkipped if credentials are missing or empty.
    """
    for path in (STATE_HOME / "feishu.json", DEFAULT_STATE_HOME / "feishu.json"):
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                log("飞书凭证文件读取失败，尝试下一个位置")
                continue
            if data.get("app_id") and data.get("app_secret"):
                return data
    raise FeishuSkipped("飞书凭证未配置（feishu.json 不存在或 app_id/app_secret 为空）")


# ---------------------------------------------------------- Access Token

def _request_json(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    method: str | None = None,
) -> dict[str, Any]:
    """Send an HTTP request and return parsed JSON. Retries up to 3 times for transient errors."""
    hdrs = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 401/403/99991663: credential errors, do not retry
            if exc.code in (401, 403):
                body = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"飞书 HTTP {exc.code}: {body[:500]}") from exc
            last_exc = exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last_exc = exc
        if attempt < 2:
            time.sleep(1 + attempt)
    raise RuntimeError(f"飞书请求失败（重试 3 次）: {last_exc}") from last_exc


def _get_access_token_new(creds: dict[str, Any]) -> tuple[str, int]:
    """New-style token endpoint (accounts.feishu.cn)."""
    payload = json.dumps({
        "grant_type": "client_credentials",
        "client_id": creds["app_id"],
        "client_secret": creds["app_secret"],
    }).encode("utf-8")
    result = _request_json("https://accounts.feishu.cn/oauth/v3/token", data=payload)
    token = result.get("access_token", "")
    expires_in = int(result.get("expires_in", 7200))
    if not token:
        # Check for error response
        err_msg = result.get("error_description") or result.get("error") or "未返回 access_token"
        raise RuntimeError(f"飞书新版凭证接口失败: {err_msg}")
    return token, expires_in


def _get_access_token_legacy(creds: dict[str, Any]) -> tuple[str, int]:
    """Legacy token endpoint (open.feishu.cn), fallback only."""
    payload = json.dumps({
        "app_id": creds["app_id"],
        "app_secret": creds["app_secret"],
    }).encode("utf-8")
    result = _request_json(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=payload,
    )
    token = result.get("tenant_access_token", "")
    expire = int(result.get("expire", 7200))
    if not token:
        msg = result.get("msg") or "未返回 tenant_access_token"
        raise RuntimeError(f"飞书旧版凭证接口失败: {msg}")
    return token, expire


def get_access_token(creds: dict[str, Any], log: Callable[[str], None] = _不记录日志) -> str:
    """Get a cached or fresh access token. Caches to feishu-token.json."""
    # Try cached token
    if TOKEN_FILE.exists():
        try:
            cached = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
            expires_at = float(cached.get("expires_at", 0))
            if time.time() < expires_at - TOKEN_MARGIN_SECONDS:
                return cached["token"]
        except (json.JSONDecodeError, KeyError, TypeError, OSError):
            pass

    # Fetch new token: try new endpoint first, fall back to legacy
    try:
        token, expires_in = _get_access_token_new(creds)
    except Exception as new_exc:
        log("飞书新版凭证接口失败，尝试旧版")
        try:
            token, expires_in = _get_access_token_legacy(creds)
        except Exception as legacy_exc:
            raise RuntimeError(
                f"飞书凭证获取全部失败: 新版={new_exc}, 旧版={legacy_exc}"
            ) from legacy_exc

    # Cache token
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    cache_data = {"token": token, "expires_at": time.time() + expires_in}
    TOKEN_FILE.write_text(json.dumps(cache_data, ensure_ascii=False), encoding="utf-8")
    return token


# ---------------------------------------------------------- Chat Lookup

def _api_get(endpoint: str, token: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    """GET request to Feishu Open API with authorization."""
    base = "https://open.feishu.cn/open-apis"
    url = f"{base}{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return _request_json(
        url,
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )


def find_chat_id(creds: dict[str, Any], token: str, state: dict[str, Any],
                 log: Callable[[str], None] = _不记录日志) -> str:
    """Find the chat_id for the configured group name. Caches in state."""
    feishu_state = state.setdefault("feishu", {})
    cached_id = feishu_state.get("chat_id", "")
    if cached_id:
        return cached_id

    chat_name = creds.get("chat_name", "")
    if not chat_name:
        raise FeishuSkipped("feishu.json 里要写 chat_name（要读的群名）")
    page_token = ""
    while True:
        params: dict[str, str] = {"page_size": "50"}
        if page_token:
            params["page_token"] = page_token
        result = _api_get("/im/v1/chats", token, params)
        # Check API-level error
        code = result.get("code", 0)
        if code != 0:
            msg = result.get("msg", "")
            raise RuntimeError(f"飞书获取群列表失败 code={code}: {msg}")
        data = result.get("data", {})
        items = data.get("items", [])
        for item in items:
            if item.get("name") == chat_name:
                chat_id = item["chat_id"]
                feishu_state["chat_id"] = chat_id
                log("找到飞书收件箱群")
                return chat_id
        if not data.get("has_more"):
            break
        page_token = data.get("page_token", "")
        if not page_token:
            break

    raise RuntimeError(f"机器人不在名为 {chat_name!r} 的群里，请确认机器人已加入该群")


# ---------------------------------------------------------- Pull Messages

def _extract_urls(content_str: str) -> list[str]:
    """Extract URLs from a message content string (handles escaped slashes)."""
    # Feishu JSON content may have escaped forward slashes
    unescaped = content_str.replace(r"\/", "/")
    urls = URL_RE.findall(unescaped)
    # Clean trailing punctuation
    cleaned = []
    for url in urls:
        url = url.rstrip(".,;，。；\"')")
        if url:
            cleaned.append(url)
    return list(dict.fromkeys(cleaned))


def _message_text(msg: dict[str, Any]) -> str:
    """Extract readable text from a message, stripping JSON structure."""
    body = msg.get("body", {})
    content_str = body.get("content", "")
    if not content_str:
        return ""
    # Try to parse as JSON and extract text fields
    try:
        content = json.loads(content_str)
        if isinstance(content, dict):
            # text type: {"text": "..."}
            if "text" in content:
                return str(content["text"])
        # post type: nested structure with text elements
        if isinstance(content, dict):
            parts: list[str] = []
            _collect_text_parts(content, parts)
            if parts:
                return " ".join(parts)
    except (json.JSONDecodeError, TypeError):
        pass
    return content_str


# 抖音「复制链接」带的口令碎片，顺序每次不一样（如 ":2pm ABC:/ 01/01 A@b.CD"、"c@d.ef :4pm 06/15 XYZ:/"）
_DOUYIN_PREFIX = re.compile(r"^\s*\d+\.\d+\s*复制打开抖音[，,]\s*看看")
_DOUYIN_CODE_TOKEN = re.compile(
    r"^(?:[A-Za-z0-9]{1,3}@[A-Za-z0-9]{1,3}\.[A-Za-z]{1,3}"  # c@d.ef、A@b.CD
    r"|:\d{1,2}[ap]m"                                          # :4pm
    r"|\d{1,2}/\d{1,2}"                                         # 06/15
    r"|[A-Za-z0-9]{2,4}:/)$"                                    # XYZ:/
)


def _strip_douyin_share_code(note: str) -> str:
    """去掉抖音分享文案开头的「1.23 复制打开抖音，看看」和口令碎片，作品标题和用户备注保留。

    口令碎片至少出现 2 个才删，单独一个像日期的「06/15」不动，免得误删用户自己写的字。
    """
    note = _DOUYIN_PREFIX.sub("", note, count=1)
    tokens = note.split()
    flags = [bool(_DOUYIN_CODE_TOKEN.match(token)) for token in tokens]
    if sum(flags) >= 2:
        tokens = [token for token, is_code in zip(tokens, flags) if not is_code]
    return " ".join(tokens)


def _collect_text_parts(obj: Any, parts: list[str]) -> None:
    """Recursively collect text values from a Feishu post/rich-text structure."""
    if isinstance(obj, dict):
        # Collect text and href values
        if obj.get("tag") == "text" and obj.get("text"):
            parts.append(str(obj["text"]))
        elif obj.get("tag") == "a" and obj.get("href"):
            parts.append(str(obj["href"]))
            if obj.get("text"):
                parts.append(str(obj["text"]))
        for value in obj.values():
            if isinstance(value, (dict, list)):
                _collect_text_parts(value, parts)
    elif isinstance(obj, list):
        for item in obj:
            _collect_text_parts(item, parts)


def pull_new_messages(
    creds: dict[str, Any],
    token: str,
    state: dict[str, Any],
    chat_id: str,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """拉取消息，返回链接条目和待提交状态，不修改传入状态。"""
    feishu_state = state.get("feishu", {})
    seen_ids: list[str] = list(feishu_state.get("seen_ids", []))
    seen_set = set(seen_ids)
    last_ts = feishu_state.get("last_message_ts", 0)

    # First run: only look back 3 days
    if not last_ts:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=FIRST_RUN_LOOKBACK_DAYS)
        start_time = str(int(cutoff.timestamp()))
    else:
        # Leave 60-second overlap to avoid missing messages
        start_time = str(max(0, int(last_ts) - OVERLAP_SECONDS))

    entries: list[dict[str, str]] = []
    page_token = ""
    total_fetched = 0
    latest_ts = int(last_ts or 0)

    while total_fetched < MAX_MESSAGES_PER_PULL:
        remaining = min(50, MAX_MESSAGES_PER_PULL - total_fetched)
        params: dict[str, str] = {
            "container_id_type": "chat",
            "container_id": chat_id,
            "start_time": start_time,
            "sort_type": "ByCreateTimeAsc",
            "page_size": str(remaining),
        }
        if page_token:
            params["page_token"] = page_token

        result = _api_get("/im/v1/messages", token, params)
        code = result.get("code", 0)
        if code != 0:
            msg = result.get("msg", "")
            raise RuntimeError(f"飞书读取群消息失败 code={code}: {msg}")

        data = result.get("data", {})
        items = data.get("items", [])
        if not items:
            break

        for msg in items:
            total_fetched += 1
            message_id = msg.get("message_id", "")
            # Skip bot's own messages
            sender = msg.get("sender", {})
            if sender.get("sender_type") == "app":
                continue
            # Dedup by message_id
            if message_id in seen_set:
                continue

            # Track timestamp for cursor
            create_time = msg.get("create_time", "0")
            try:
                msg_ts = int(create_time) // 1000  # milliseconds → seconds
            except (ValueError, TypeError):
                msg_ts = 0
            if msg_ts > latest_ts:
                latest_ts = msg_ts

            # Extract URLs from message content
            body = msg.get("body", {})
            content_str = body.get("content", "")
            urls = _extract_urls(content_str)
            if not urls:
                # Mark as seen even if no URLs
                seen_set.add(message_id)
                seen_ids.append(message_id)
                continue

            # Get readable text for the note
            text = _message_text(msg)
            # Remove URLs from note text, clean up
            note = text
            for url in urls:
                note = note.replace(url, "")
            if any(re.match(r"https?://(?:www\.)?v\.douyin\.com/", url, re.I) for url in urls):
                note = _strip_douyin_share_code(note)
            note = re.sub(r"\s+", " ", note).strip()
            if len(note) > MAX_NOTE_LENGTH:
                note = note[:MAX_NOTE_LENGTH]

            # Format timestamp
            msg_time = ""
            if msg_ts:
                msg_time = datetime.fromtimestamp(
                    msg_ts, tz=timezone.utc
                ).astimezone().strftime("%Y-%m-%d %H:%M")

            for url in urls:
                entries.append({"note": note, "url": url, "time": msg_time})

            seen_set.add(message_id)
            seen_ids.append(message_id)

        if not data.get("has_more"):
            break
        page_token = data.get("page_token", "")
        if not page_token:
            break

    # Update state: keep only the most recent MAX_SEEN_IDS
    if len(seen_ids) > MAX_SEEN_IDS:
        seen_ids = seen_ids[-MAX_SEEN_IDS:]
    pending_state: dict[str, Any] = {"seen_ids": seen_ids}
    if latest_ts:
        pending_state["last_message_ts"] = latest_ts

    return entries, pending_state


# ---------------------------------------------------------- Inbox Append

def append_to_inbox(entries: list[dict[str, str]], inbox_path: Path) -> int:
    """Append link entries to the link inbox file.

    Each entry becomes one line: - 飞书 2026-10-27 21:05 <note text with url>
    Returns the number of lines appended.
    """
    if not entries:
        return 0

    lines_to_add: list[str] = []
    for entry in entries:
        note = entry.get("note", "")
        url = entry["url"]
        msg_time = entry.get("time", "")
        # Format: - 飞书 YYYY-MM-DD HH:MM <note> <url>
        parts = ["- 飞书"]
        if msg_time:
            parts.append(msg_time)
        if note:
            parts.append(note)
        # Ensure URL is in the line (sync_mobile uses URL_RE to find it)
        if url not in note:
            parts.append(url)
        lines_to_add.append(" ".join(parts))

    # Read existing content to avoid corrupting it
    existing = ""
    if inbox_path.exists():
        existing = inbox_path.read_text(encoding="utf-8")

    # Append with newline
    suffix = "\n".join(lines_to_add) + "\n"
    if existing and not existing.endswith("\n"):
        suffix = "\n" + suffix

    with inbox_path.open("a", encoding="utf-8") as f:
        f.write(suffix)

    return len(lines_to_add)


# ---------------------------------------------------------- Main Entry

def sync_feishu_inbox(state: dict[str, Any], inbox_path: Path,
                      log: Callable[[str], None] = _不记录日志) -> int:
    """Pull new messages from Feishu and append links to inbox.

    Returns number of links appended. Raises FeishuSkipped if credentials
    are not configured (caller should handle gracefully).
    All network/auth errors are raised as RuntimeError.
    """
    creds = load_credentials(log)
    token = get_access_token(creds, log)
    chat_id = find_chat_id(creds, token, state, log)
    entries, pending_state = pull_new_messages(creds, token, state, chat_id)
    count = append_to_inbox(entries, inbox_path)
    state.setdefault("feishu", {}).update(pending_state)
    return count
