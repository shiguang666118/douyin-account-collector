# 账号拆解初稿（开源版）
#
# 读一份收录结果（runs/<ID>/list.md、comments.md）+ 视频文字卡，调 DeepSeek（OpenAI 兼容接口）
# 写一份账号拆解初稿，文件开头标「AI 初稿，待审」。
#
# 密钥只从文件读（config 里的 deepseek_api_key_file），不打印、不写进代码。
# 用法见同目录 README.md。
import argparse
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_FILE = Path(os.environ.get("ACCOUNT_CONFIG", "") or (HERE / "config.json"))

DEFAULTS = {
    "deepseek_base_url": "https://api.deepseek.com",
    "deepseek_model": "deepseek-chat",
    "deepseek_api_key_file": "~/.config/deepseek/api_key",
    "cards_dir": "./vault/02_内容素材/视频",
    "breakdown_dir": "./vault/01_对标账号",
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.is_file():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    return cfg


def _read_key(cfg: dict) -> str:
    path = Path(os.path.expanduser(str(cfg["deepseek_api_key_file"])))
    return path.read_text(encoding="utf-8").strip()


def deepseek_chat(messages: list[dict], cfg: dict) -> str:
    url = str(cfg["deepseek_base_url"]).rstrip("/") + "/chat/completions"
    body = {
        "model": cfg["deepseek_model"],
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 8000,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + _read_key(cfg),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ValueError(f"DeepSeek 接口返回 {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"连不上 DeepSeek 接口：{exc.reason}") from exc
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "")


def card_texts(cards_dir: Path, sec: str) -> list[str]:
    out: list[str] = []
    if not Path(cards_dir).is_dir():
        return out
    for path in sorted(Path(cards_dir).glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if f'account_sec_uid: "{sec}"' not in text:
            continue
        body = text.split("---", 2)[-1].strip()
        title_match = re.search(r"^# (.+)$", body, re.M)
        title = title_match.group(1).strip() if title_match else path.stem
        transcript = re.sub(r"^# .*$", "", body, flags=re.M).strip()
        out.append(f"### {title}\n{transcript[:1500]}")
    return out


def safe(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|#^\[\]\n\r\t]+', " ", name or "").strip()[:40]
    return name or "博主"


def write_analysis(nickname: str, sec: str, runs_dir: Path, cfg: dict | None = None, platform: str = "抖音") -> Path:
    cfg = cfg or load_config()
    runs = Path(runs_dir)
    list_md = (
        (runs / "list.md").read_text(encoding="utf-8", errors="replace")
        if (runs / "list.md").is_file() else "（作品表未生成）"
    )
    comments_md = (
        (runs / "comments.md").read_text(encoding="utf-8", errors="replace")
        if (runs / "comments.md").is_file() else "（没拉评论）"
    )
    cards = card_texts(Path(os.path.expanduser(str(cfg["cards_dir"]))), sec)
    cards_block = "\n\n".join(cards[:20]) if cards else "（没找到来源卡）"

    user_content = (
        f"博主：{nickname}\n\n"
        f"=== 作品列表（含数据）===\n{list_md[:20000]}\n\n"
        f"=== 高赞视频评论 ===\n{comments_md[:8000]}\n\n"
        f"=== 各条视频文字稿（自动转写，有错字）===\n{cards_block[:50000]}\n"
    )
    draft = deepseek_chat(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        cfg,
    )
    if not draft.strip():
        raise ValueError("DeepSeek 没返回内容")
    day = datetime.now().strftime("%Y-%m-%d")
    out_dir = Path(os.path.expanduser(str(cfg["breakdown_dir"])))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"拆解_{safe(nickname)}_{day}.md"
    if path.exists():  # 同一天重复收同一个号：不覆盖已有拆解
        path = out_dir / f"拆解_{safe(nickname)}_{day}_{datetime.now():%H%M}.md"
    path.write_text(
        f"# 拆解：{nickname}（{platform}）{day}\n\n"
        f"> AI 初稿，待审。数据来自自动收录，引用和判断未经人工核对；批量收录不抽画面，画面/剪辑需看原视频。\n\n"
        f"{draft.strip()}\n",
        encoding="utf-8",
    )
    return path


SYSTEM_PROMPT = """根据提供的作品列表、文字稿和评论，用 Markdown 汇总账号内容。注明资料范围，区分观察结果与推测。"""


def main() -> None:
    ap = argparse.ArgumentParser(description="按收录结果写账号拆解初稿")
    ap.add_argument("--nickname", required=True, help="博主名")
    ap.add_argument("--sec", required=True, help="账号 sec_uid")
    ap.add_argument("--runs", required=True, help="收录结果目录（含 list.md / comments.md）")
    ap.add_argument("--platform", default="抖音", help="抖音 / 小红书，只影响拆解标题")
    ap.add_argument("--out", default="", help="拆解写到哪（默认 config 的 breakdown_dir）")
    args = ap.parse_args()
    cfg = load_config()
    if args.out:
        cfg["breakdown_dir"] = args.out
    path = write_analysis(args.nickname, args.sec, Path(args.runs), cfg, args.platform)
    print(f"拆解初稿：{path}")


if __name__ == "__main__":
    main()
