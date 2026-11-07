"""视频拆解素材包：把一条视频压成「少量文字 + 一张画面墙」，给 AI 读，省 token。
全程本地，不联网、不上传。

用法（用装好 whisperX 的 Python 运行）：
  python -X utf8 brief.py 视频.mp4 [--title 标题] [--stats "赞 1 / 评 2 / 藏 3 / 转 4"] [--out 输出目录] [--window 10]

输出目录（默认：视频同目录下 <视频名>_brief）：
  brief.md   基本信息 + 开头原文 + 按 window 秒合并的带时间稿
  wall.jpg   9 格画面墙：前 3 格是开头 0.5/1.5/3 秒（看钩子），后 6 格均匀分布
没有同名 .srt 时，调用环境变量 SUBTITLE_PY 指定的字幕脚本（whisperX，GPU）。
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# 出字幕的脚本：环境变量 SUBTITLE_PY 优先，否则用同目录下的 make_subs.py
SUBTITLE_PY = Path(os.environ.get("SUBTITLE_PY", "") or (Path(__file__).resolve().parent / "make_subs.py"))
WINGET_FFMPEG = Path(os.environ.get("FFMPEG_DIR", "") or (Path.home() / r"AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/ffmpeg-8.1.1-full_build/bin"))
PUNCT = "，。！？、；：,.!?;:…—~～“”\"'‘’（）()《》【】 \n"


def tool(name):
    p = shutil.which(name)
    if p:
        return p
    cand = WINGET_FFMPEG / (name + ".exe")
    if cand.exists():
        return str(cand)
    sys.exit(f"找不到 {name}，请先装 ffmpeg")


def probe(video):
    out = subprocess.run(
        [tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-show_entries", "format=duration",
         "-of", "default=nw=1", str(video)],
        capture_output=True, text=True, encoding="utf-8").stdout
    d = {k: v for k, v in (l.split("=", 1) for l in out.split() if "=" in l)}
    return int(d["width"]), int(d["height"]), float(d["duration"])


def parse_srt(path):
    """返回 [(开始秒, 文字, 结束秒)]。"""
    cues = []
    for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8-sig").strip()):
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        m = re.match(r"(\d+):(\d+):(\d+),(\d+) --> (\d+):(\d+):(\d+),(\d+)", lines[1])
        if not m:
            continue
        h, mi, s, ms, h2, mi2, s2, ms2 = map(int, m.groups())
        cues.append((h * 3600 + mi * 60 + s + ms / 1000, " ".join(lines[2:]).strip(),
                     h2 * 3600 + mi2 * 60 + s2 + ms2 / 1000))
    return cues


def join_cues(cues):
    """把字幕行拼回成文字。10-28 改：原来每行后面都加逗号，可字幕行是按每行字数硬切的，
    会切在词中间（"创作系，统""Agen，t"）。现在只在两行之间真有停顿（≥0.3 秒）、
    且上一行没带标点时才补逗号（英文单词被切开时也是直接接上）。"""
    out = ""
    prev_end = None
    for start, txt, end in cues:
        if out:
            if out[-1] not in PUNCT and prev_end is not None and start - prev_end >= 0.3:
                out += "，"
        out += txt
        prev_end = end
    if out and out[-1] not in PUNCT:
        out += "。"
    return out


def mmss(t):
    return f"{int(t)//60:02d}:{int(t)%60:02d}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--title", default="")
    ap.add_argument("--stats", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--window", type=float, default=10)
    a = ap.parse_args()

    video = Path(a.video).resolve()
    out = Path(a.out) if a.out else video.with_name(video.stem + "_brief")
    out.mkdir(parents=True, exist_ok=True)

    w, h, dur = probe(video)

    srt = video.with_suffix(".srt")
    if not srt.exists():
        print("没有字幕，本地出字幕中……")
        subprocess.run([sys.executable, "-X", "utf8", str(SUBTITLE_PY), str(video)], check=True)
    cues = parse_srt(srt)

    # 按 window 秒合并成段，少占 token
    segs, cur_start, cur = [], None, []
    for cue in cues:
        t = cue[0]
        if cur_start is None:
            cur_start = t
        if t - cur_start >= a.window and cur:
            segs.append((cur_start, join_cues(cur)))
            cur_start, cur = t, []
        cur.append(cue)
    if cur:
        segs.append((cur_start, join_cues(cur)))

    full = "".join(txt for _, txt, _ in cues)
    nchar = len([c for c in full if c not in PUNCT])
    pace = nchar / (dur / 60) if dur else 0
    hook = join_cues([cue for cue in cues if cue[0] < 8])

    # 画面墙：前三格看钩子，后六格均匀分布
    times = [0.5, 1.5, 3.0] + [dur * (i + 1) / 7 for i in range(6)]
    frames = []
    # 11-02 改：截图和拼图放在系统临时目录做，拼好再挪回 out。原来放在 out/_frames，
    # 文件夹名带 %（如"99%的人…"）时 ffmpeg 把 % 当序号占位符，拼画面墙报错，整条收录失败
    tmp = Path(tempfile.mkdtemp(prefix="brief_frames_"))
    try:
        for i, t in enumerate(times):
            f = tmp / f"{i}.jpg"
            subprocess.run([tool("ffmpeg"), "-y", "-v", "error", "-ss", f"{min(t, dur-0.1):.2f}", "-i", str(video),
                            "-frames:v", "1", "-vf", "scale=480:-2", str(f)], check=True)
            frames.append(f)
        subprocess.run([tool("ffmpeg"), "-y", "-v", "error", "-i", str(tmp / "%d.jpg"), "-vf", "tile=3x3",
                        "-frames:v", "1", "-q:v", "4", str(tmp / "wall.jpg")], check=True)
        shutil.move(str(tmp / "wall.jpg"), str(out / "wall.jpg"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    md = [f"# {a.title or video.stem}", ""]
    md += [f"- 文件：{video.name}", f"- 时长：{mmss(dur)}（{dur:.0f} 秒），画幅 {w}×{h}（{'横屏' if w>=h else '竖屏'}）",
           f"- 字数：约 {nchar}，语速约 {pace:.0f} 字/分钟"]
    if a.stats:
        md.append(f"- 数据：{a.stats}")
    md += ["", "## 开头 8 秒原文（钩子）", "", hook or "【无字幕内容】", "",
           f"## 带时间稿（每 {a.window:.0f} 秒一段；自动识别，可能有错字）", ""]
    md += [f"[{mmss(t)}] {txt}" for t, txt in segs]
    md += ["", "## 画面墙", "", "wall.jpg：左上到右下 9 格，前 3 格是开头 0.5 / 1.5 / 3 秒，其余 6 格均匀分布。", ""]
    (out / "brief.md").write_text("\n".join(md), encoding="utf-8")
    print(f"完成：{out}")


if __name__ == "__main__":
    main()
