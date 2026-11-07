# 抖音 / 小红书 博主全部作品收录（开源版）
#
# 主页名片 → 作品列表 → 逐条下载 + 本地转写 → 来源卡写进指定文件夹。
# 依赖 video_capture.py（TikHub 取视频地址、下载、本地转写），不调任何云端审核/模型。
#
# 用法见同目录 README.md。所有路径都从环境变量或 config.json 读，没有写死的个人路径。
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import video_capture as vc  # noqa: E402

_CFG: dict = {}
if (HERE / "config.json").is_file():
    try:
        _CFG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _CFG = {}

# 卡片写到哪：环境变量 ACCOUNT_VAULT 优先，其次 config.json 的 vault，最后用同目录下的 vault/
VAULT = Path(os.environ.get("ACCOUNT_VAULT", "") or _CFG.get("vault") or (HERE / "vault"))
VIDEO_DIR = Path(
    os.environ.get("ACCOUNT_CARDS_DIR", "")
    or _CFG.get("cards_dir")
    or (VAULT / "02_内容素材" / "视频")
)
if os.environ.get("ACCOUNT_BATCH_TEST_DIR"):  # 自测用：卡片写到临时子目录
    VIDEO_DIR = VAULT / os.environ["ACCOUNT_BATCH_TEST_DIR"]
RUNS = Path(__file__).resolve().parent / "runs"
_RUN_OUT: Path = RUNS  # 当前任务的运行目录（详情 JSON 放这里）
EP_PROFILE = "/api/v1/douyin/app/v3/handler_user_profile"
EP_POSTS = "/api/v1/douyin/app/v3/fetch_user_post_videos"
EP_COMMENTS = "/api/v1/douyin/app/v3/fetch_video_comments"
# 单价 2026-11-04 用 /api/v1/tikhub/user/get_endpoint_info 查的，都是 $0.001/次
vc.ENDPOINT_COST.update({EP_PROFILE: 0.001, EP_POSTS: 0.001, EP_COMMENTS: 0.001})
SEC_RE = re.compile(r"(?:sec_uid=|/share/user/|/user/)(MS4wLjAB[\w-]+)")
UA_MOBILE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"


def say(*parts):
    print(*parts, flush=True)


def resolve_sec_uid(text: str) -> str:
    """接受：主页分享短链 v.douyin.com/xxx、主页网址、sec_uid 本身、或整段分享文案。"""
    m = SEC_RE.search(text)
    if m:
        return m.group(1)
    if text.startswith("MS4wLjAB"):
        return text.strip()
    url = re.search(r"https?://\S+", text)
    if not url:
        raise SystemExit(f"认不出主页链接：{text}")
    proc = subprocess.run(["curl", "-sI", "-A", UA_MOBILE, url.group(0).rstrip("，。")],
                          capture_output=True, text=True, timeout=30)
    m = SEC_RE.search(proc.stdout)
    if not m:
        raise SystemExit(f"短链没跳到博主主页（可能是单条视频链接，不是名片）：{url.group(0)}")
    return m.group(1)


def feishu_cards(days: int) -> list[tuple[str, str]]:
    """只读飞书群最近 days 天的消息，挑出主页名片（文案里有“查看TA的更多作品”）。不改采集状态。"""
    import feishu_inbox as f
    creds = f.load_credentials()
    tok = f.get_access_token(creds)
    state_file = Path.home() / ".local/state/rays-brain/state.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    chat = state.get("feishu", {}).get("chat_id") or f.find_chat_id(creds, tok, {})
    params = {"container_id_type": "chat", "container_id": chat, "sort_type": "ByCreateTimeDesc",
              "page_size": "50", "start_time": str(int(time.time()) - 86400 * days)}
    r = f._api_get("/im/v1/messages", tok, params)
    found = []
    for m in r.get("data", {}).get("items", []):
        text = m.get("body", {}).get("content", "")
        if "更多作品" in text:
            when = datetime.fromtimestamp(int(m.get("create_time", "0")) // 1000).strftime("%m-%d %H:%M")
            url = re.search(r"https?://v\.douyin\.com/[\w-]+/?", text.replace("\\/", "/"))
            if url:
                found.append((when, url.group(0)))
    return found


def fetch_all(sec: str, tk: dict, out: Path) -> tuple[dict, list]:
    prof = vc.tikhub_get(EP_PROFILE, {"sec_user_id": sec}, tk)
    vc.log_cost(EP_PROFILE, "sec_uid:" + sec, prof.get("code") == 200)
    user = (prof.get("data") or {}).get("user") or {}
    if not user:
        raise SystemExit("TikHub 没返回博主资料")
    items, cursor = [], 0
    for _ in range(30):  # 每页 20 条，最多 600 条
        r = vc.tikhub_get(EP_POSTS, {"sec_user_id": sec, "max_cursor": str(cursor), "count": "20"}, tk)
        vc.log_cost(EP_POSTS, "sec_uid:" + sec, r.get("code") == 200)
        d = r.get("data") or {}
        items += d.get("aweme_list") or []
        if not d.get("has_more"):
            break
        cursor = d.get("max_cursor")
        time.sleep(1)
    seen, uniq = set(), []
    for it in items:
        if it["aweme_id"] not in seen:
            seen.add(it["aweme_id"])
            uniq.append(it)
    uniq.sort(key=lambda x: x["create_time"])
    (out / "profile.json").write_text(json.dumps(prof, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "posts.json").write_text(json.dumps(uniq, ensure_ascii=False, indent=1), encoding="utf-8")
    return user, uniq


def existing_cards() -> dict[str, str]:
    """视频目录里已经有的作品（不管是谁收的、哪个平台）：作品 ID → 卡片文件名。"""
    found = {}
    for p in VIDEO_DIR.glob("*.md"):
        m = re.search(r'platform_item_id: "([^"]+)"', p.read_text(encoding="utf-8", errors="replace")[:3000])
        if m:
            found[m.group(1)] = p.stem
    return found


def safe(text: str) -> str:
    text = re.sub(r'[\\/:*?"<>|#^\[\]\n\r\t%]+', " ", text)  # % 会让 ffmpeg 抽帧出错
    return re.sub(r"\s+", " ", text).strip()[:50] or "视频"


def capture_one(it: dict, user: dict, sec: str, config: dict) -> str:
    aid = str(it["aweme_id"])
    url = f"https://www.douyin.com/video/{aid}"
    short = hashlib.sha256(f"douyin:{aid}".encode()).hexdigest()[:8]
    day = datetime.now().strftime("%Y-%m-%d")
    cap = vc.capture(url, VIDEO_DIR, lambda name: f"{day}-{safe(name)}-{short}", VAULT, config)
    nick, fans = user.get("nickname", ""), user.get("follower_count", "")
    # 批量收录不出画面墙（用户 11-06 定）：卡片正文去掉这一段，抽出来的 wall.jpg 也删掉。
    # 转写和下载不动；取文案、飞书收录那两条路不经过这里，行为不变。
    body = re.sub(r"\n## 画面墙\n.*?(?=\n## )", "", cap["body"], flags=re.S)
    media_rel = cap["meta"].get("media_dir")
    if media_rel:
        (VAULT / media_rel / "wall.jpg").unlink(missing_ok=True)
    lines = [
        "---",
        f"title: {json.dumps(cap['title'], ensure_ascii=False)}",
        f"source_url: {json.dumps(url)}",
        f"created_at: {json.dumps(datetime.now().astimezone().isoformat(timespec='seconds'))}",
        "tags:",
        "  - benchmark-account",
        "capture_source: account-batch",
        f"account_sec_uid: {json.dumps(sec)}",
        "processing_status: transcript-only",
        "fetch_status: success",
        f"content_type: {json.dumps(cap['content_type'])}",
        *[f"{k}: {json.dumps(str(v), ensure_ascii=False)}" for k, v in cap["meta"].items()],
        f"author_fans: {json.dumps(str(fans))}",
        "---",
        "",
        f"# {cap['title']}",
        "",
        f"原链接：{url}",
        "",
        f"> 对标账号批量收录（{nick}，账号粉丝 {fans}，{day} 收录）：只出文案，不出素材卡。",
        "",
        body,
    ]
    (VIDEO_DIR / f"{cap['stem']}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return cap["stem"]


def write_list(user: dict, items: list, cards: dict, out: Path) -> None:
    rows = [f"# {user.get('nickname')} 作品列表（{datetime.now():%Y-%m-%d %H:%M} 拉取）", "",
            f"- 粉丝 {user.get('follower_count')} / 获赞 {user.get('total_favorited')} / 作品数 {user.get('aweme_count')}"
            f" / {user.get('ip_location', '')}",
            "- 简介：" + str(user.get("signature", "")).replace("\n", " / "), "",
            "| # | 发布 | 标题（去掉话题） | 时长 | 赞 / 评 / 藏 / 转 | 画幅 | 置顶 | 来源卡 |",
            "|---|---|---|---|---|---|---|---|"]
    for i, it in enumerate(items, 1):
        s, v = it.get("statistics") or {}, it.get("video") or {}
        d = round((v.get("duration") or 0) / 1000)
        desc = re.sub(r"#\S+", "", it.get("desc") or "").strip().replace("|", "／").replace("\n", " ")
        stem = cards.get(str(it["aweme_id"]))
        link = f"[[{stem}\\|卡片]]" if stem else "未收录"
        rows.append(f"| {i} | {vc.iso_from_ts(it['create_time'])[:10]} | {desc} | {d // 60}:{d % 60:02d} | "
                    f"{s.get('digg_count')} / {s.get('comment_count')} / {s.get('collect_count')} / {s.get('share_count')} | "
                    f"{v.get('width')}x{v.get('height')} | {'是' if it.get('is_top') else ''} | {link} |")
    (out / "list.md").write_text("\n".join(rows) + "\n", encoding="utf-8")


def fetch_comments(items: list, n: int, tk: dict, out: Path) -> None:
    top = sorted(items, key=lambda x: -((x.get("statistics") or {}).get("digg_count") or 0))[:n]
    md = ["# 高赞视频第一页评论（按评论点赞排序）", ""]
    for it in top:
        aid = str(it["aweme_id"])
        r = vc.tikhub_get(EP_COMMENTS, {"aweme_id": aid, "cursor": "0", "count": "20"}, tk)
        vc.log_cost(EP_COMMENTS, "aweme:" + aid, r.get("code") == 200)
        cs = (r.get("data") or {}).get("comments") or []
        title = re.sub(r"#\S+", "", it.get("desc") or "").strip()[:40]
        md += [f"## {title}（{aid}，赞 {it['statistics']['digg_count']}）", ""]
        for c in sorted(cs, key=lambda c: -(c.get("digg_count") or 0)):
            text = (c.get("text") or "").replace("\n", " ").strip()
            if text:
                md.append(f"- [{c.get('digg_count')} 赞 / {c.get('reply_comment_total')} 回复] {text}")
        md.append("")
        time.sleep(1)
    (out / "comments.md").write_text("\n".join(md) + "\n", encoding="utf-8")


# ---------- 小红书（2026-11-06 加，接口按 Claude 实测 + TikHub 官方文档） ----------
XHS_API = "/api/v1/xiaohongshu/app_v2"
EP_XHS_USER = XHS_API + "/get_user_info"
EP_XHS_NOTES = XHS_API + "/get_user_posted_notes"
EP_XHS_IMAGE = XHS_API + "/get_image_note_detail"
EP_XHS_VIDEO = XHS_API + "/get_video_note_detail"
EP_XHS_COMMENTS = XHS_API + "/get_note_comments"
XHS_COST = 0.01
for _ep in (EP_XHS_USER, EP_XHS_NOTES, EP_XHS_IMAGE, EP_XHS_VIDEO, EP_XHS_COMMENTS):
    vc.ENDPOINT_COST[_ep] = XHS_COST

XHS_USER_RE = re.compile(r"/user/profile/([0-9a-f]{24})")
XHS_NOTE_RE = re.compile(r"/(?:explore|discovery/item)/([0-9a-f]{24})")
XHS_ID_RE = re.compile(r"^[0-9a-f]{24}$")


def detect_platform(text: str) -> str:
    text = text or ""
    if re.search(r"(?:v\.douyin\.com|douyin\.com|iesdouyin\.com)", text):
        return "douyin"
    if re.search(r"(?:xhslink\.cn|xhslink\.com|xiaohongshu\.com)", text):
        return "xiaohongshu"
    return ""


def resolve_xhs_user(text: str) -> str:
    """主页分享短链 / 主页网址 / user_id → 24 位 user_id。只看跳转后的路径，不看文案关键词。"""
    text = (text or "").strip()
    if XHS_ID_RE.match(text):
        return text
    url = re.search(r"https?://\S+", text)
    if not url:
        raise SystemExit(f"认不出主页链接：{text}")
    link = url.group(0).rstrip("，。")
    # xhslink 的 HEAD 是 404，必须 GET；跳转后的页面里 user_id 藏在 JSON 里，
    # 而且被转义成 \u002F / \/ / &#x2F;，要先还原再认。这一步不花钱。
    proc = subprocess.run(
        ["curl", "-sL", "-A", UA_MOBILE, link], capture_output=True, text=True, timeout=45
    )
    body = (proc.stdout or "").replace("\\u002F", "/").replace("\\/", "/").replace("&#x2F;", "/")
    m = XHS_USER_RE.search(body)
    if m:
        return m.group(1)
    if XHS_NOTE_RE.search(body) or "/explore/" in link or "discovery/item" in link:
        raise SystemExit("这是单条笔记链接，不是博主主页（名片）")
    raise SystemExit(f"短链没跳到博主主页：{link}")


def _deep_get(obj, *paths):
    """按给定路径列表依次尝试，返回第一个拿到的值。路径用元组表示，如 ("data", "data", "notes")。"""
    for path in paths:
        cur = obj
        ok = True
        for key in path:
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                ok = False
                break
        if ok and cur not in (None, "", [], {}):
            return cur
    return None


def _xhs_note_detail(raw: dict) -> dict:
    """详情接口的真实结构：data.data 是列表，列表[0].note_list[0] 才是笔记本身。"""
    inner = _deep_get(raw, ("data", "data"), ("data",))
    if isinstance(inner, list):
        if not inner:
            return {}
        first = inner[0]
        if isinstance(first, dict):
            notes = first.get("note_list")
            if isinstance(notes, list) and notes and isinstance(notes[0], dict):
                return notes[0]
            return first
        return {}
    if isinstance(inner, dict):
        notes = inner.get("note_list")
        if isinstance(notes, list) and notes and isinstance(notes[0], dict):
            return notes[0]
        return inner
    return {}


def fetch_xhs_account(user_id: str, tk: dict, out: Path) -> tuple[dict, list]:
    prof = vc.tikhub_get(EP_XHS_USER, {"user_id": user_id}, tk)
    vc.log_cost(EP_XHS_USER, "user:" + user_id, prof.get("code") == 200)
    time.sleep(1)
    user = _deep_get(prof, ("data", "data", "user"), ("data", "user"), ("data", "data"))
    if not isinstance(user, dict) or not (user.get("nickname") or user.get("nick_name")):
        # 有的返回直接把 user 字段平铺
        user = _deep_get(prof, ("data", "data"), ("data",)) or {}
    if not user:
        raise SystemExit("TikHub 没返回小红书博主资料")
    notes, cursor = [], ""
    for _ in range(30):  # 一页约 22 条，最多 660 条
        params = {"user_id": user_id}
        if cursor:
            params["cursor"] = cursor
        page = vc.tikhub_get(EP_XHS_NOTES, params, tk)
        vc.log_cost(EP_XHS_NOTES, "user:" + user_id, page.get("code") == 200)
        time.sleep(1)
        inner = _deep_get(page, ("data", "data"), ("data",)) or {}
        batch = (inner.get("notes") if isinstance(inner, dict) else None) or []
        notes += batch
        if not (isinstance(inner, dict) and inner.get("has_more")) or not batch:
            break
        cursor = str(batch[-1].get("cursor") or "")
        if not cursor:
            break
    seen, uniq = set(), []
    for note in notes:
        nid = str(note.get("id") or "")
        if nid and nid not in seen:
            seen.add(nid)
            uniq.append(note)
    uniq.sort(key=lambda x: x.get("create_time") or 0)
    (out / "profile.json").write_text(json.dumps(prof, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "posts.json").write_text(json.dumps(uniq, ensure_ascii=False, indent=1), encoding="utf-8")
    return user, uniq


def xhs_stats(note: dict) -> tuple:
    return (
        note.get("likes"),
        note.get("comments_count"),
        note.get("collected_count"),
        note.get("share_count"),
    )


def xhs_video_urls(note: dict) -> list[str]:
    stream = _deep_get(note, ("video_info_v2", "media", "stream")) or {}
    urls: list[str] = []
    for codec in ("h264", "h265"):
        for item in stream.get(codec) or []:
            url = item.get("master_url")
            if url and url not in urls:
                urls.append(url)
    return urls


def xhs_duration(note: dict) -> int:
    return int(_deep_get(note, ("video_info_v2", "capa", "duration")) or 0)


def xhs_note_desc(note: dict) -> str:
    return str(note.get("desc") or "")


def xhs_note_title(note: dict) -> str:
    for key in ("display_title", "title", "desc"):
        value = str(note.get(key) or "").strip()
        if value:
            return value
    return str(note.get("id") or "笔记")


def capture_xhs_note(note: dict, user: dict, user_id: str, config: dict, tk: dict,
                     full_desc: bool = False) -> str:
    nid = str(note["id"])
    is_video = (note.get("type") == "video") or bool(xhs_video_urls(note))
    title = xhs_note_title(note)
    day = datetime.now().strftime("%Y-%m-%d")
    short = hashlib.sha256(("xiaohongshu:" + nid).encode()).hexdigest()[:8]
    stem = f"{day}-{safe(title)}-{short}"
    media_dir = VIDEO_DIR / stem
    media_dir.mkdir(parents=True, exist_ok=True)
    url = f"https://www.xiaohongshu.com/explore/{nid}"
    nick = str(user.get("nickname") or user.get("nick_name") or "")
    fans = user.get("fans") or user.get("follower_count") or ""
    like, comment, collect, share = xhs_stats(note)
    minutes, seconds = divmod(xhs_duration(note), 60)
    stats_text = f"赞 {like} / 评 {comment} / 藏 {collect} / 转 {share}"
    desc = str(xhs_note_desc(note) or "").strip()
    content_type = "video-transcript"
    detail: dict = {}
    if not is_video or full_desc:
        ep = EP_XHS_VIDEO if is_video else EP_XHS_IMAGE
        raw = vc.tikhub_get(ep, {"note_id": nid}, tk)
        vc.log_cost(ep, "note:" + nid, raw.get("code") == 200)
        time.sleep(1)
        (out_dir_for(nid)).write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
        detail = _xhs_note_detail(raw)
    # 详情里的正文是全的；列表里的只有前 100 字（视频常常只剩话题）
    full = str(detail.get("desc") or "").strip()
    if full and len(full) > len(desc):
        desc = full
    body: list[str] = [
        f"- 平台：小红书　作者：{nick or '未知'}",
        f"- 发布：{datetime.fromtimestamp(int(note.get('create_time') or 0)).strftime('%Y-%m-%d %H:%M')}"
        + (f"　时长：{minutes}:{seconds:02d}" if is_video else ""),
        f"- 赞/评/藏/转（收录时）：{stats_text}",
        "",
        "## 作品描述",
        "",
        desc or "（无）",
        "",
    ]
    if is_video and not full_desc:
        body += ["（列表里的正文只有前 100 字，正文截断；要看全文加 --full-desc）", ""]
    if is_video:
        urls = xhs_video_urls(note)
        if not urls:
            raise RuntimeError("列表里没有视频地址")
        video = vc.download(urls, media_dir / "video.mp4", "xiaohongshu")
        brief = vc.run_brief(video, media_dir, title, stats_text, config)
        rel_dir = media_dir.relative_to(VAULT).as_posix()
        body += [
            "## 画面墙",
            "",
            f"![[{rel_dir}/wall.jpg]]",
            "",
            "左上到右下 9 格：前 3 格是开头 0.5 / 1.5 / 3 秒，其余 6 格均匀分布。",
            "",
            "## 文字稿（本地自动识别，可能有错字）",
            "",
            vc.transcript_from_brief(brief.read_text(encoding="utf-8", errors="replace")),
        ]
    else:
        content_type = "xhs-note-text"
        images = detail.get("images_list") or detail.get("image_list") or []
        if not images:
            images = note.get("images_list") or []
        rel_dir = media_dir.relative_to(VAULT).as_posix()
        if images:
            body += ["## 图片（最多 9 张）", ""]
            for i, img in enumerate(images[:9], 1):
                src = _deep_get(img, ("url",), ("url_size_large",), ("original",),
                                ("url_default",), ("url_pre",))
                if not src:
                    continue
                name = f"img_{i}.jpg"
                try:
                    vc.download([src], media_dir / name, "xiaohongshu")
                    body.append(f"![[{rel_dir}/{name}]]")
                except Exception as exc:
                    body.append(f"- 第 {i} 张下载失败：{str(exc)[:60]}")
            body += ["", "图片里的文字没有识别。", ""]
    lines = [
        "---",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        f"source_url: {json.dumps(url)}",
        f"created_at: {json.dumps(datetime.now().astimezone().isoformat(timespec='seconds'))}",
        "tags:",
        "  - benchmark-account",
        "capture_source: account-batch",
        f"platform: {json.dumps('xiaohongshu')}",
        f"platform_item_id: {json.dumps(nid)}",
        f"account_sec_uid: {json.dumps(user_id)}",
        "processing_status: transcript-only",
        "fetch_status: success",
        f"content_type: {json.dumps(content_type)}",
        f"author: {json.dumps(nick, ensure_ascii=False)}",
        f"author_fans: {json.dumps(str(fans), ensure_ascii=False)}",
        "---",
        "",
        f"# {title}",
        "",
        f"原链接：{url}",
        "",
        "> 对标账号批量收录（小红书），只出文案，不出素材卡。",
        "",
        "\n".join(body),
    ]
    (VIDEO_DIR / f"{stem}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return stem


def out_dir_for(nid: str) -> Path:
    """详情 JSON 存到当前项目的运行目录下，失败时好对字段。"""
    return _RUN_OUT / f"detail_{nid}.json"


def write_xhs_list(user: dict, notes: list, cards: dict, out: Path) -> None:
    nick = user.get("nickname") or user.get("nick_name") or ""
    rows = [
        f"# {nick} 作品列表（{datetime.now():%Y-%m-%d %H:%M} 拉取）",
        "",
        f"- 粉丝 {user.get('fans') or user.get('follower_count')}　笔记 {len(notes)} 条　"
        f"{user.get('ip_location', '')}",
        "- 简介：" + str(user.get("desc") or user.get("signature") or "").replace("\n", " / "),
        "",
        "| # | 发布 | 标题（去掉话题） | 类型 | 赞 / 评 / 藏 / 转 | 置顶 | 来源卡 |",
        "|---|---|---|---|---|---|---|",
    ]
    for index, note in enumerate(notes, 1):
        like, comment, collect, share = xhs_stats(note)
        desc = re.sub(r"#\S+", "", xhs_note_title(note)).strip().replace("|", "／").replace("\n", " ")[:60]
        kind = "视频" if (note.get("type") == "video" or xhs_video_urls(note)) else "图文"
        stem = cards.get(str(note.get("id")))
        link = f"[[{stem}\\|卡片]]" if stem else "未收录"
        stamp = datetime.fromtimestamp(int(note.get("create_time") or 0)).strftime("%Y-%m-%d")
        rows.append(
            f"| {index} | {stamp} | {desc} | {kind} | {like} / {comment} / {collect} / {share} "
            f"| {'是' if note.get('sticky') else ''} | {link} |"
        )
    (out / "list.md").write_text("\n".join(rows) + "\n", encoding="utf-8")


def fetch_xhs_comments(notes: list, count: int, tk: dict, out: Path) -> None:
    def likes(n):
        try:
            return int(n.get("likes") or 0)
        except (TypeError, ValueError):
            return 0
    top = sorted(notes, key=likes, reverse=True)[:count]
    lines = ["# 高赞笔记第一页评论（按评论点赞排序）", ""]
    for note in top:
        nid = str(note.get("id"))
        page = vc.tikhub_get(EP_XHS_COMMENTS, {"note_id": nid}, tk)
        vc.log_cost(EP_XHS_COMMENTS, "note:" + nid, page.get("code") == 200)
        time.sleep(1)
        comments = _deep_get(page, ("data", "data", "comments"), ("data", "comments")) or []
        lines += [f"## {xhs_note_title(note)[:40]}（{nid}，赞 {note.get('likes')}）", ""]
        for c in sorted(comments, key=lambda x: -(x.get("like_count") or x.get("likes") or 0)):
            text = str(c.get("content") or c.get("text") or "").replace("\n", " ").strip()
            if text:
                lines.append(f"- [{c.get('like_count') or c.get('likes') or 0} 赞] {text}")
        lines.append("")
    (out / "comments.md").write_text("\n".join(lines) + "\n", encoding="utf-8")



def run_douyin(a) -> None:
    sec = resolve_sec_uid(a.target)
    tk = vc.load_tikhub()
    out = RUNS / sec[-12:]
    out.mkdir(parents=True, exist_ok=True)
    user, items = fetch_all(sec, tk, out)
    nick = user.get("nickname", "")
    (out / "account.txt").write_text(f"{nick}\n{sec}\n", encoding="utf-8")
    say(f"博主：{nick}　粉丝 {user.get('follower_count')}　主页显示作品 {user.get('aweme_count')} 条，接口拿到 {len(items)} 条")
    say(f"运行记录目录：{out}")
    cards = existing_cards()

    if not a.list_only:
        config = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
        todo = [it for it in items if str(it["aweme_id"]) not in cards]
        say(f"已在知识库：{len(items) - len(todo)} 条；要新收：{len(todo)} 条" + (f"（这一轮最多 {a.max} 条）" if a.max else ""))
        if a.top:
            todo = sorted(
                todo, key=lambda x: -((x.get("statistics") or {}).get("digg_count") or 0)
            )[: a.top]
        elif a.recent:
            todo = sorted(todo, key=lambda x: -(x.get("create_time") or 0))[: a.recent]
        elif a.max:
            todo = todo[:a.max]
        results = json.loads((out / "results.json").read_text(encoding="utf-8")) if (out / "results.json").exists() else {}
        for i, it in enumerate(todo, 1):
            aid = str(it["aweme_id"])
            t0 = time.time()
            try:
                stem = capture_one(it, user, sec, config)
                cards[aid] = stem
                results[aid] = {"status": "done", "card": stem}
                say(f"[{i}/{len(todo)}] 完成 {stem}（{time.time() - t0:.0f} 秒）")
            except Exception as exc:  # 单条失败不影响其他条，下次再跑会重试
                results[aid] = {"status": "failed", "error": str(exc)[:300]}
                say(f"[{i}/{len(todo)}] 失败 {aid}：{str(exc)[:200]}")
            (out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
            time.sleep(3)

    write_list(user, items, cards, out)
    if a.comments:
        fetch_comments(items, a.comments, tk, out)
    missing = [str(it["aweme_id"]) for it in items if str(it["aweme_id"]) not in cards]
    say(f"作品表：{out / 'list.md'}" + (f"；评论：{out / 'comments.md'}" if a.comments else ""))
    say(f"SUMMARY platform=douyin id={sec} posts={len(items)} have={len(items) - len(missing)} "
        f"missing={len(missing)} videos={len(items)} images=0 nickname={nick}")
    say(f"汇总：共 {len(items)} 条，知识库里已有 {len(items) - len(missing)} 条，还缺 {len(missing)} 条" +
        (f"（{', '.join(missing[:10])}{' …' if len(missing) > 10 else ''}）" if missing else ""))


def _xhs_take(todo: list, limit: int) -> list:
    """--max N：先各拿一条视频和图文，再按原顺序补齐，方便验证两种卡片。"""
    if not limit or limit <= 0:
        return todo
    videos = [n for n in todo if (n.get("type") == "video" or xhs_video_urls(n))]
    images = [n for n in todo if not (n.get("type") == "video" or xhs_video_urls(n))]
    picked = []
    while len(picked) < limit and (videos or images):
        if videos:
            picked.append(videos.pop(0))
        if len(picked) < limit and images:
            picked.append(images.pop(0))
    return picked


def run_xhs(a) -> None:
    global _RUN_OUT
    user_id = resolve_xhs_user(a.target)
    tk = vc.load_tikhub()
    out = RUNS / user_id[-12:]
    out.mkdir(parents=True, exist_ok=True)
    _RUN_OUT = out
    user, notes = fetch_xhs_account(user_id, tk, out)
    nick = str(user.get("nickname") or user.get("nick_name") or "")
    (out / "account.txt").write_text(f"{nick}\n{user_id}\n", encoding="utf-8")
    videos = [n for n in notes if (n.get("type") == "video" or xhs_video_urls(n))]
    images = [n for n in notes if n not in videos]
    say(f"博主：{nick}　粉丝 {user.get('fans') or user.get('follower_count')}"
        f"　笔记 {len(notes)} 条（视频 {len(videos)} / 图文 {len(images)}）")
    say(f"运行记录目录：{out}")
    pages = max(1, math.ceil(len(notes) / 22)) if notes else 1
    list_cost = XHS_COST * (1 + pages)
    est = list_cost + XHS_COST * len(images) + (XHS_COST * a.comments if a.comments else 0)
    say(f"预计 TikHub 花费：约 ${est:.2f}（列表已花 ${list_cost:.2f}；图文每条详情 $0.01）")
    cards = existing_cards()
    if a.list_only:
        write_xhs_list(user, notes, cards, out)
        missing = [n for n in notes if str(n.get("id")) not in cards]
        say(f"作品表：{out / 'list.md'}；知识库里已有 {len(notes) - len(missing)} 条，还缺 {len(missing)} 条")
        say(f"SUMMARY platform=xiaohongshu id={user_id} posts={len(notes)} "
            f"have={len(notes) - len(missing)} missing={len(missing)} "
            f"videos={len(videos)} images={len(images)} nickname={nick}")
        return
    if est > 1 and not a.yes:
        say("预计超过 $1，没加 --yes 就停在这里（列表的钱已经花了，卡片没建）")
        return
    config_file = HERE / "config.json"
    config = json.loads(config_file.read_text(encoding="utf-8")) if config_file.is_file() else {}
    todo = [n for n in notes if str(n.get("id")) not in cards]
    if a.top:
        todo = sorted(todo, key=lambda x: -(int(x.get("likes") or 0)))[: a.top]
    elif a.recent:
        todo = sorted(todo, key=lambda x: -(x.get("create_time") or 0))[: a.recent]
    elif a.max:
        todo = _xhs_take(todo, a.max)
    say(f"已在知识库：{len(notes) - len(todo)} 条；要新收：{len(todo)} 条")
    results = json.loads((out / "results.json").read_text(encoding="utf-8")) if (out / "results.json").exists() else {}
    fails: dict[str, int] = {}
    for i, note in enumerate(todo, 1):
        nid = str(note.get("id"))
        if fails.get(nid, 0) >= 3:
            continue
        t0 = time.time()
        try:
            stem = capture_xhs_note(note, user, user_id, config, tk, full_desc=a.full_desc)
            cards[nid] = stem
            results[nid] = {"status": "done", "card": stem}
            fails.pop(nid, None)
            say(f"[{i}/{len(todo)}] 完成 {stem}（{time.time() - t0:.0f} 秒）")
        except Exception as exc:  # 单条失败不影响别的，下次再跑会重试；同一条连续 3 次就跳过
            fails[nid] = fails.get(nid, 0) + 1
            results[nid] = {"status": "failed", "error": str(exc)[:300], "attempts": fails[nid]}
            say(f"[{i}/{len(todo)}] 失败 {nid}（第 {fails[nid]} 次）：{str(exc)[:200]}")
        (out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        time.sleep(3)
    write_xhs_list(user, notes, cards, out)
    if a.comments:
        fetch_xhs_comments(notes, a.comments, tk, out)
    missing = [str(n.get("id")) for n in notes if str(n.get("id")) not in cards]
    say(f"作品表：{out / 'list.md'}" + (f"；评论：{out / 'comments.md'}" if a.comments else ""))
    say(f"SUMMARY platform=xiaohongshu id={user_id} posts={len(notes)} "
        f"have={len(notes) - len(missing)} missing={len(missing)} "
        f"videos={len(videos)} images={len(images)} nickname={nick}")


def main() -> None:
    ap = argparse.ArgumentParser(description="博主全部作品收录（抖音 / 小红书）")
    ap.add_argument("target", nargs="?", help="主页分享短链 / 主页网址 / sec_uid / user_id；不填就用 --feishu")
    ap.add_argument("--feishu", action="store_true", help="只列出飞书群最近的主页名片（抖音），不下载")
    ap.add_argument("--days", type=int, default=3, help="--feishu 往前看几天，默认 3")
    ap.add_argument("--list-only", action="store_true", help="只拉作品列表和资料，不下载转写")
    ap.add_argument("--max", type=int, default=0, help="这一轮最多新收几条（0 = 全部）；小红书会先各拿一条视频和图文")
    ap.add_argument("--top", type=int, default=0, help="只收点赞最高的 N 条")
    ap.add_argument("--recent", type=int, default=0, help="只收最近的 N 条")
    ap.add_argument("--comments", type=int, default=0, help="给点赞最高的 N 条拉第一页评论")
    ap.add_argument("--full-desc", action="store_true", help="小红书视频笔记也查详情拿全文（每条多 $0.01）")
    ap.add_argument("--yes", action="store_true", help="预计花费超过 $1 时也继续")
    ap.add_argument("--resolve-only", action="store_true", help="只解析主页链接，打印平台和 ID，不调收费接口")
    a = ap.parse_args()

    if a.resolve_only:
        plat = detect_platform(a.target)
        if plat == "douyin":
            ident = resolve_sec_uid(a.target)
        elif plat == "xiaohongshu":
            ident = resolve_xhs_user(a.target)
        else:
            raise SystemExit(f"认不出是抖音还是小红书链接：{a.target}")
        say(f"SUMMARY platform={plat} id={ident} posts=0 have=0 missing=0 videos=0 images=0 nickname=")
        return

    if a.feishu:
        cards = feishu_cards(a.days)
        say(f"飞书群最近 {a.days} 天的主页名片：{len(cards)} 张")
        for when, url in cards:
            say(f"  {when}  {url}")
        return
    if not a.target:
        ap.error("要给主页链接，或者先用 --feishu 找名片")

    platform = detect_platform(a.target)
    if platform == "xiaohongshu":
        run_xhs(a)
    elif platform == "douyin":
        run_douyin(a)
    else:
        raise SystemExit(f"认不出是抖音还是小红书链接：{a.target}")


if __name__ == "__main__":
    main()
