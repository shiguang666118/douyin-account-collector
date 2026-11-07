# 第三方代码说明

本目录的 `video_capture.py` 和 `feishu_inbox.py` 来自开源项目 **rays-brain-kit**，
该项目以 MIT 许可证发布：

```
MIT License

Copyright (c) 2026 Ray
```

我们在原文件基础上做了少量本机适配（路径改为相对/环境变量、去掉个人路径），
逻辑没有重写。两处改动：`run_brief` 的默认解释器和脚本改为本目录，其余保持原样。

其他文件（`account_batch.py`、`brief.py`、`analysis.py`）为本项目原创，同样以 MIT 发布，见 `LICENSE`。
