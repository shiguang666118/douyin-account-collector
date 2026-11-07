# 抖音 / 小红书 博主收录（开源版）

发一张博主的主页名片（或主页链接），把这个博主的**全部作品**下载、本地转写、出作品表和评论，可选再让 DeepSeek 写一份**账号拆解初稿**。

面向做 AI 自媒体、需要研究对标账号的人。全程本地跑，视频不上传。

## 它能做什么

- 抖音、小红书主页名片 → 作品列表（发布日期、标题、时长、赞 / 评 / 藏 / 转、是否置顶）
- 逐条下载 + 本地转写 → 每条一张文字卡（只有文字稿，**不出画面墙**）
- 小红书图文笔记：正文全文 + 最多 9 张图片下载并嵌进卡片
- 生成 `list.md`（作品表）、`comments.md`（高赞视频第一页评论）
- 可选：调 DeepSeek 写账号拆解初稿，文件开头标「AI 初稿，待审」
- 按作品 ID 跳过已经收过的，随时停、下次接着收，不重复下载

## 它不做什么

- **画面墙不一致**：抖音批量收录不生成；小红书视频笔记会生成一张（转写流程自带，不需要可以删掉卡片里那段）
- **不删视频**（转写完 `video.mp4` 留着，想删自己删）
- 小红书视频笔记的**正文只有前 100 字**（列表接口限的），要全文加 `--full-desc`（每条多 $0.01）
- 小红书**图片里的文字不识别**，只嵌图，不做 OCR
- 图集作品没有视频，卡片里只有作品描述和图片
- 不代替人工判断：数据是收录当天的，转写有错字，拆解是初稿

## 依赖

| 东西 | 用途 | 必须？ |
|---|---|---|
| Python 3.10+（Windows 建议 3.12，加 `-X utf8`） | 跑脚本 | 必须 |
| ffmpeg（PATH 里能找到） | 下载后处理 | 必须 |
| TikHub API key | 拉博主资料、作品列表、视频地址、评论 | 必须（按次付费） |
| 本地转写（whisperX / FunASR，或现成的同名 `.srt`） | 出文字稿 | 转写必须有，可以用现成字幕绕过 |
| DeepSeek API key | 写拆解初稿 | 可选 |

## 安装

1. 装 Python 3.10+ 和 ffmpeg，确认 `ffmpeg -version` 能跑。
2. 复制 `config.example.json` 为 `config.json`，按你的目录改。
3. 放 TikHub key：复制 `tikhub.example.json` 为 `tikhub.json`，填 `api_key`（也可以放到 `~/.local/state/rays-brain/tikhub.json`）。
4. 可选：DeepSeek key 放到 `~/.config/deepseek/api_key`（一行，只放 key），或改 `config.json` 里的 `deepseek_api_key_file`。

## 用法

```bash
# 只拉作品列表，看共几条、已收几条（不下载）
python -X utf8 account_batch.py <主页链接> --list-only

# 小红书主页（xhslink 短链或 xiaohongshu.com/user/profile/... 都行）
python -X utf8 account_batch.py https://xhslink.cn/o/xxxx --list-only

# 只解析链接、打印平台和 ID，不调收费接口
python -X utf8 account_batch.py <主页链接> --resolve-only

# 收全部作品 + 拉 5 条高赞评论 + 写拆解初稿
python -X utf8 account_batch.py <主页链接> --comments 5 --analysis

# 只收点赞最高的 10 条
python -X utf8 account_batch.py <主页链接> --top 10

# 这一轮最多收 8 条（没收完的下次再跑会接着收）
python -X utf8 account_batch.py <主页链接> --max 8

# 只收最近 20 条
python -X utf8 account_batch.py <主页链接> --recent 20

# 配了飞书的话，列飞书群最近的主页名片
python -X utf8 account_batch.py --feishu
```

产出：

- 卡片：`<cards_dir>/<日期>-<标题>-<短哈希>.md`，同名文件夹里放 `video.mp4`、`brief.md`、`video.srt`
- 运行记录：`runs/<sec_uid 后 12 位>/`，含 `list.md`、`comments.md`、`profile.json`、`posts.json`、`results.json`
- 拆解：`<breakdown_dir>/拆解_<博主名>_<日期>.md`

## 费用

- **TikHub（抖音）**：博主资料 1 次 + 作品列表每 20 条 1 次 + 每条视频 1 次 + 每条评论 1 次，约 $0.001/次。30 条作品的号全收约 $0.04。
- **TikHub（小红书）每次调用 $0.01**：资料 1 次 + 列表每页约 22 条 1 次 + 每条图文详情 1 次 + 每条评论 1 次。视频笔记不额外调接口（列表里就带视频地址），所以 30 条视频的号约 $0.04～0.05，图文多的会更高。
- **DeepSeek**：写一次拆解几分钱（按 token 计）。
- 转写在本机，不花钱。

## 隐私与合规

- 代码里**没有任何密钥**；`config.json`、`tikhub.json`、`runs/`、`vault/` 默认都不进 Git
- 密钥只在调用时从文件读，不打印、不写日志
- 视频只在本机转写，不上传
- 抓的是公开内容；批量抓取可能违反平台规则，用的人自己承担风险；拆解只用于学习，不要搬运别人的内容

## 许可

MIT。`video_capture.py`、`feishu_inbox.py` 来自开源项目 rays-brain-kit（MIT, Copyright (c) 2026 Ray），致谢见 `NOTICES.md`。
