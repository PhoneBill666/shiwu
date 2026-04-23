# 十五 (shiwu)

一个运行在 macOS 上的本地 AI 助手，基于 Apple Silicon MLX 框架，使用 Qwen3.5-9B 模型进行推理，所有数据和模型均在本地运行。

## 功能

- 中文对话，支持思考模式（Qwen3.5 深度推理）
- 长期记忆系统：自动从对话中提取并记住用户偏好、身份、项目背景等
- 联网搜索：模型可自主判断是否需要搜索，也可手动 `/search`、`/fetch`
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
