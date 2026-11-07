#!/usr/bin/env python3
"""抖音、小红书视频链接 → TikHub 取作品数据和视频地址 → 下载 → video-brief 本地转写 + 画面墙。

这个模块被 account_batch.py 调用；也可以单独用 capture() 抓一条视频。
TikHub 凭据读 $RAYS_BRAIN_STATE/tikhub.json（找不到时退回 ~/.local/state/rays-brain/tikhub.json）。
每次 TikHub 调用按单价记进 $RAYS_BRAIN_STATE/logs/tikhub-cost.jsonl。
转写全在本机（video-brief → whisperX），不上传视频。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

STATE_HOME = Path(os.environ.get("RAYS_BRAIN_STATE", str(Path.home() / ".local/state/rays-brain")))
DEFAULT_STATE_HOME = Path.home() / ".local/state/rays-brain"
COST_LOG = STATE_HOME / "logs" / "tikhub-cost.jsonl"

DOUYIN_BY_SHARE = "/api/v1/douyin/app/v3/fetch_one_video_by_share_url"
XHS_VIDEO_NOTE = "/api/v1/xiaohongshu/app_v2/get_video_note_detail"
XHS_IMAGE_NOTE = "/api/v1/xiaohongshu/app_v2/get_image_note_detail"
# 单价（美元/次）：2026-10-27 用 /api/v1/tikhub/user/get_endpoint_info 查的 endpoint_cost
ENDPOINT_COST = {DOUYIN_BY_SHARE: 0.001, XHS_VIDEO_NOTE: 0.01, XHS_IMAGE_NOTE: 0.01}

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)
REFERER = {"douyin": "https://www.douyin.com/", "xiaohongshu": "https://www.xiaohongshu.com/"}
XHS_NOTE_ID_RE = re.compile(r"(?:explore|discovery/item|item)/([0-9a-f]{24})")


class VideoCaptureError(RuntimeError):
    pass


def detect_platform(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc.lower()
    if host.endswith(("douyin.com", "iesdouyin.com")):
        return "douyin"
    if host.endswith(("xiaohongshu.com", "xhslink.com", "xhslink.cn")):
        return "xiaohongshu"
    return ""


def load_tikhub() -> dict[str, Any]:
    for path in (STATE_HOME / "tikhub.json", DEFAULT_STATE_HOME / "tikhub.json"):
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("api_key"):
                return data
    raise VideoCaptureError("缺少 TikHub 凭据（tikhub.json）")


def log_cost(endpoint: str, url: str, ok: bool) -> float:
    cost = ENDPOINT_COST.get(endpoint, 0.01)
    COST_LOG.parent.mkdir(parents=True, exist_ok=True)
    with COST_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "endpoint": endpoint, "url": url, "ok": ok, "cost_usd": cost,
        }, ensure_ascii=False) + "\n")
    return cost


def tikhub_get(endpoint: str, params: dict[str, str], tikhub: dict[str, Any]) -> dict[str, Any]:
    base = str(tikhub.get("base_url") or "https://api.tikhub.io").rstrip("/")
    request = urllib.request.Request(
        f"{base}{endpoint}?{urllib.parse.urlencode(params)}",
        headers={"Authorization": f"Bearer {tikhub['api_key']}", "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # 401/402/403/429 是凭据、余额、权限、限速问题，重试没用
        raise VideoCaptureError(f"TikHub HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise VideoCaptureError(f"TikHub 请求失败：{exc}") from exc


def iso_from_ts(value: Any) -> str:
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return ""
    if ts > 10**12:
        ts //= 1000
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def first_line_title(text: str, limit: int = 30) -> str:
    line = next((part.strip() for part in str(text or "").splitlines() if part.strip()), "")
    line = re.sub(r"#[^#\s]+(?:\[话题\])?#?", "", line).strip()
    # 抖音描述常是「标题 正文……」挤在一行：在中文字符后的空格处截断（英文词之间的空格不算）。
    # 只在前面已经够长（≥12 字）时截，以保留短标题中的中英文混排
    for gap in re.finditer(r"(?<=[^\x00-\x7f])\s+", line):
        if gap.start() >= 12:
            line = line[:gap.start()]
            break
    return line[:limit]


def file_safe(title: str) -> str:
    """文件名去掉 emoji 和组合符（Windows 下 curl、ffmpeg 等外部工具对它们不稳）。"""
    cleaned = "".join(
        ch for ch in title
        if ord(ch) <= 0xFFFF and ord(ch) not in (0xFE0F, 0x200D, 0x20E3) and not 0x2600 <= ord(ch) <= 0x27BF
    )
    return re.sub(r"\s+", " ", cleaned).strip() or "视频"


def parse_douyin(response: dict[str, Any]) -> dict[str, Any]:
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    item = data.get("aweme_detail") or data.get("aweme_info") or {}
    if not isinstance(item, dict) or not item:
        reason = ""
        filters = data.get("filter_list") if isinstance(data, dict) else None
        if isinstance(filters, list) and filters and isinstance(filters[0], dict):
            reason = str(filters[0].get("detail_msg") or filters[0].get("reason") or "")
        raise VideoCaptureError(f"抖音没有返回作品数据{('：' + reason) if reason else ''}")
    video = item.get("video") or {}
    urls: list[str] = []
    for key in ("play_addr_h264", "play_addr", "download_addr"):
        urls.extend(u for u in ((video.get(key) or {}).get("url_list") or []) if u not in urls)
    stats = item.get("statistics") or {}
    author = item.get("author") or {}
    desc = str(item.get("desc") or "")
    duration = video.get("duration") or item.get("duration") or 0
    return {
        "platform": "douyin",
        "item_id": str(item.get("aweme_id") or ""),
        "title": first_line_title(desc) or "抖音视频",
        "desc": desc,
        "author": str(author.get("nickname") or ""),
        "published_at": iso_from_ts(item.get("create_time")),
        "duration_seconds": round(int(duration) / 1000) if int(duration or 0) > 1000 else int(duration or 0),
        "stats": {
            "like": stats.get("digg_count"), "comment": stats.get("comment_count"),
            "collect": stats.get("collect_count"), "share": stats.get("share_count"),
        },
        "video_urls": urls,
        "note_type": "video" if urls else "unknown",
    }


def pick_xhs_item(response: dict[str, Any], note_id: str) -> dict[str, Any]:
    outer = response.get("data") if isinstance(response.get("data"), dict) else {}
    items = outer.get("data")
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list) or not items:
        raise VideoCaptureError(f"小红书没有返回笔记数据：{outer.get('msg') or outer.get('message') or ''}")
    # 图文接口的笔记包在 note_list 里（10-28 用网页链接的 note_id 实测），先摊平
    flat: list[Any] = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("note_list"), list):
            flat.extend(note for note in item["note_list"] if isinstance(note, dict))
        else:
            flat.append(item)
    items = flat or items
    # 接口会把推荐笔记一起返回；有笔记 ID 时只认 ID 对得上的那条。
    # 10-28 修：对不上就报错（交给图文接口再试），原来会拿第一条推荐顶替，抓成别人的视频
    if note_id:
        for item in items:
            if isinstance(item, dict) and item.get("id") == note_id:
                return item
        raise VideoCaptureError("小红书返回里没有这条笔记（只有推荐笔记），可能是图文笔记或链接失效")
    return items[0] if isinstance(items[0], dict) else {}


def parse_xhs(item: dict[str, Any]) -> dict[str, Any]:
    if not item:
        raise VideoCaptureError("小红书返回的笔记为空")
    stream = (((item.get("video_info_v2") or {}).get("media") or {}).get("stream") or {})
    urls: list[str] = []
    for codec in ("h264", "h265", "av1"):
        for entry in stream.get(codec) or []:
            for u in [entry.get("master_url"), *(entry.get("backup_urls") or [])]:
                if u and u not in urls:
                    urls.append(u)
    capa = ((item.get("video_info_v2") or {}).get("capa") or {})
    user = item.get("user") or {}
    desc = str(item.get("desc") or "")
    return {
        "platform": "xiaohongshu",
        "item_id": str(item.get("id") or ""),
        "title": str(item.get("title") or "").strip() or first_line_title(desc) or "小红书笔记",
        "desc": desc,
        "author": str(user.get("nickname") or ""),
        "published_at": iso_from_ts(item.get("time")),
        "duration_seconds": int(capa.get("duration") or 0),
        "stats": {
            "like": item.get("liked_count"), "comment": item.get("comments_count"),
            "collect": item.get("collected_count"), "share": item.get("shared_count"),
        },
        "video_urls": urls,
        "note_type": str(item.get("type") or ("video" if urls else "normal")),
    }


def fetch_info(url: str, tikhub: dict[str, Any], call: Callable[..., dict[str, Any]] = tikhub_get) -> tuple[dict[str, Any], float]:
    platform = detect_platform(url)
    if platform == "douyin":
        response = call(DOUYIN_BY_SHARE, {"share_url": url}, tikhub)
        try:
            info = parse_douyin(response)
        except VideoCaptureError:
            log_cost(DOUYIN_BY_SHARE, url, False)
            raise
        return info, log_cost(DOUYIN_BY_SHARE, url, True)
    if platform == "xiaohongshu":
        match = XHS_NOTE_ID_RE.search(url)
        note_id = match.group(1) if match else ""
        params = {"note_id": note_id} if note_id else {"share_text": url}
        cost = 0.0
        try:
            response = call(XHS_VIDEO_NOTE, params, tikhub)
            info = parse_xhs(pick_xhs_item(response, note_id))
            cost += log_cost(XHS_VIDEO_NOTE, url, True)
            if info["video_urls"]:
                return info, cost
        except VideoCaptureError:
            cost += log_cost(XHS_VIDEO_NOTE, url, False)
        # 视频接口拿不到（多半是图文笔记）：再调一次图文接口拿正文
        response = call(XHS_IMAGE_NOTE, params, tikhub)
        try:
            info = parse_xhs(pick_xhs_item(response, note_id))
        except VideoCaptureError:
            log_cost(XHS_IMAGE_NOTE, url, False)
            raise
        cost += log_cost(XHS_IMAGE_NOTE, url, True)
        info["video_urls"] = []
        return info, cost
    raise VideoCaptureError(f"不是抖音或小红书链接：{url}")


def download(urls: list[str], dest: Path, platform: str) -> Path:
    curl = shutil.which("curl") or "curl"
    # Windows 自带 curl 按系统代码页转路径，带 emoji 的路径写不进去：先下到纯英文临时文件再挪
    temp = STATE_HOME / "tmp" / f"video-{os.getpid()}.mp4"
    temp.parent.mkdir(parents=True, exist_ok=True)
    errors = []
    for link in urls[:6]:
        proc = subprocess.run(
            [curl, "-fsSL", "--max-time", "300", "-A", UA, "-e", REFERER.get(platform, ""), "-o", str(temp), link],
            capture_output=True, timeout=330,
        )
        if proc.returncode == 0 and temp.exists() and temp.stat().st_size > 50_000:
            dest.parent.mkdir(parents=True, exist_ok=True)  # 下成功才建文件夹，失败不留空目录
            shutil.move(str(temp), str(dest))
            return dest
        errors.append(proc.stderr.decode(errors="replace").strip()[:120] or f"curl {proc.returncode}")
    temp.unlink(missing_ok=True)
    raise VideoCaptureError("视频下载失败：" + "；".join(errors[:3]))


def stats_text(stats: dict[str, Any]) -> str:
    return "/".join(str(stats.get(k) if stats.get(k) is not None else "?") for k in ("like", "comment", "collect", "share"))


def run_funasr_srt(video: Path, config: dict[str, Any]) -> bool:
    """本机加（10-28）：先用阿里 FunASR（Fun-ASR-Nano）出同名 .srt，brief.py 看到字幕就不再跑 Whisper。
    没装、出错、超时都返回 False，交给 brief.py 原来的 Whisper 兜底。"""
    if config.get("asr_engine", "funasr") != "funasr":
        return False
    python = Path(str(config.get("funasr_python") or sys.executable))
    script = Path(str(config.get("funasr_script") or ""))
    srt = video.with_suffix(".srt")
    if not python.is_file() or not script.is_file():
        return False
    try:
        proc = subprocess.run(
            [str(python), "-X", "utf8", str(script), str(video), "--out", str(srt)],
            capture_output=True, timeout=int(config.get("funasr_timeout_seconds", 1800)),
        )
    except subprocess.TimeoutExpired:
        proc = None
    # 正常跑完但字幕是空的 = 没人说话（纯音乐、只有画面字幕），就用这个空结果，别再让 Whisper 去"幻听"
    if proc is None or proc.returncode != 0 or not srt.exists():
        srt.unlink(missing_ok=True)
        return False
    return True


def run_brief(video: Path, out_dir: Path, title: str, stats: str, config: dict[str, Any]) -> Path:
    run_funasr_srt(video, config)
    python = str(config.get("brief_python") or sys.executable)
    script = str(config.get("brief_script") or (Path(__file__).resolve().parent / "brief.py"))
    if not Path(python).exists() or not Path(script).exists():
        raise VideoCaptureError(f"找不到转写工具：{python} / {script}")
    proc = subprocess.run(
        [python, "-X", "utf8", script, str(video), "--title", title, "--stats", stats, "--out", str(out_dir)],
        capture_output=True, timeout=int(config.get("brief_timeout_seconds", 1800)),
    )
    brief = out_dir / "brief.md"
    if proc.returncode != 0 or not brief.exists():
        tail = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()[-3:]
        raise VideoCaptureError("转写失败：" + " | ".join(tail))
    return brief


# 本机加（10-28）：语音识别没听到人声（纯音乐、只有画面字幕的视频）时会“幻听”——把给它的提示语
# （字幕工具 make_subs.py 里的“以下是普通话口播，使用简体中文和标点。”）原样吐出来，
# 或者编出训练数据里常见的固定字幕（MINGPAO CANADA 之类）。这些不是文案，要去掉。
PROMPT_ECHOES = ("以下是普通话口播", "使用简体中文和标点")
HALLUCINATION_MARKS = ("MINGPAO", "INGPAO", "明镜与点点", "点点栏目", "AMARA.ORG", "YOYO TELEVISION", "字幕志愿者", "请不吝点赞")
TIMED_LINE_RE = re.compile(r"^(\[\d{1,2}:\d{2}(?::\d{2})?\]\s*)(.*)$")
_NOT_WORD_RE = re.compile(r"[^\w]+")


def clean_asr_text(text: str) -> str:
    """去掉提示语回声；整段都是幻听时返回空串。"""
    for echo in PROMPT_ECHOES:
        text = text.replace(echo, "")
    core = _NOT_WORD_RE.sub("", text)
    if not core or any(mark in core.upper() for mark in HALLUCINATION_MARKS):
        return ""
    return re.sub(r"^[\s，。,.、]+", "", text).strip()


def transcript_from_brief(text: str) -> str:
    """brief.md 去掉标题和画面墙说明，只留基本信息、开头原文和带时间稿；去掉幻听的句子。"""
    lines = text.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    body = "\n".join(lines)
    body = body.split("\n## 画面墙", 1)[0]
    cleaned: list[str] = []
    section = ""
    for line in body.splitlines():
        if line.startswith("## "):
            section = line
            cleaned.append(line)
            continue
        timed = TIMED_LINE_RE.match(line)
        if timed:
            words = clean_asr_text(timed.group(2))
            if words:
                cleaned.append(timed.group(1) + words)
            continue
        if section.startswith("## 开头") and line.strip() and not line.startswith("【"):
            cleaned.append(clean_asr_text(line) or "【没识别到口播】")
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def capture(
    url: str, folder: Path, name_for: Callable[[str], str], vault: Path, config: dict[str, Any]
) -> dict[str, Any]:
    """返回 title、body、content_type、meta（写进来源卡 frontmatter 的附加字段）、cost_usd、stem。

    name_for(标题) 给出来源卡文件名（不含 .md）；folder/<同名>/ 里放 video.mp4（不进 Git）、brief.md、wall.jpg。
    """
    tikhub = load_tikhub()
    info, cost = fetch_info(url, tikhub)
    stem = name_for(file_safe(info["title"]))
    meta = {
        "platform": info["platform"],
        "platform_item_id": info["item_id"],
        "author": info["author"],
        "source_published_at": info["published_at"],
        "duration_seconds": str(info["duration_seconds"]),
        "stats_like_comment_collect_share": stats_text(info["stats"]),
        "note_type": info["note_type"],
        "tikhub_cost_usd": f"{cost:.3f}",
    }
    header = [
        f"- 平台：{'抖音' if info['platform'] == 'douyin' else '小红书'}　作者：{info['author'] or '未知'}",
        f"- 发布：{info['published_at'][:16].replace('T', ' ') or '未知'}　时长：{info['duration_seconds']} 秒",
        f"- 赞/评/藏/转（收录时）：{stats_text(info['stats'])}",
        "",
        "## 作品描述",
        "",
        info["desc"].strip() or "（无）",
        "",
    ]
    if not info["video_urls"]:
        return {
            "title": info["title"], "content_type": "xhs-note-text", "meta": meta, "cost_usd": cost, "stem": stem,
            "body": "\n".join(header + ["（这条是图文笔记或没拿到视频地址，只有上面的文字。）"]),
        }
    media_dir = folder / stem
    video = download(info["video_urls"], media_dir / "video.mp4", info["platform"])
    brief = run_brief(video, media_dir, info["title"], stats_text(info["stats"]), config)
    if not config.get("keep_video", True):
        video.unlink(missing_ok=True)
    rel_dir = media_dir.relative_to(vault).as_posix()
    body = header + [
        "## 画面墙",
        "",
        f"![[{rel_dir}/wall.jpg]]",
        "",
        "左上到右下 9 格：前 3 格是开头 0.5 / 1.5 / 3 秒，其余 6 格均匀分布。",
        "",
        "## 文字稿（本地自动识别，可能有错字）",
        "",
        transcript_from_brief(brief.read_text(encoding="utf-8", errors="replace")),
    ]
    meta["media_dir"] = rel_dir
    return {"title": info["title"], "content_type": "video-transcript", "meta": meta, "cost_usd": cost, "stem": stem, "body": "\n".join(body)}
