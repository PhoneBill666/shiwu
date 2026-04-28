# 十五 (shiwu)

一个运行在 macOS 上的本地 AI 助手，基于 Apple Silicon MLX 框架，使用 Qwen3.5-9B 模型进行推理，所有数据和模型均在本地运行。

## 功能

- 中文对话，支持思考模式（Qwen3.5 深度推理）
- 长期记忆系统：自动从对话中提取并记住用户偏好、身份、项目背景等
- 联网搜索：模型可自主判断是否需要搜索，也可手动 `/search`、`/fetch`
- Canvas 日程：支持通过 Canvas Calendar Feed 查看临近作业 due、考试和近期安排
- 文件阅读：支持 PDF、Word、纯文本等，通过 `@文件名` 或 `[/路径]` 引用
- 多模型切换

## 环境要求

- macOS（Apple Silicon）
- Python 3.10+

## 安装

```bash
# 克隆项目
git clone <repo-url> && cd shiwu

# 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

## 运行

```bash
source .venv/bin/activate
python main.py
```

首次运行会自动下载模型（约 5GB），之后会从缓存加载。

输入 `/help` 查看所有可用命令。

## Canvas Calendar Feed

如果你学校的 Canvas 只能提供 Calendar Feed 链接，可以直接接入：

```env
CANVAS_FEED_URL="https://your-canvas-feed-url"
```

```bash
/canvas set-feed <你的 Canvas Calendar Feed 链接>
/canvas
```

说明：

- 推荐优先把链接放进 `.env` 的 `CANVAS_FEED_URL`
- `/canvas` 默认查看未来 14 天安排
- `/canvas 7` 查看未来 7 天安排
- `/canvas status` 查看当前是否已配置 feed
- `/canvas clear-feed` 清除当前配置
- 在普通对话里提到 `Canvas`、`日程`、`作业 due`、`考试`、`ddl` 等词时，助手也会自动抓取近期安排辅助回答
