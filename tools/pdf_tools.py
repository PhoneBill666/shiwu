"""PDF 工具：合并等操作。使用 pdfunite (Poppler)。"""

import os
import glob
import subprocess


def pdf_merge(folder: str, output: str) -> str:
    """合并指定文件夹下所有 PDF 为一个文件。"""
    folder = os.path.expanduser(folder)
    if not os.path.isdir(folder):
        return f"文件夹不存在: {folder}"

    pdf_files = sorted(glob.glob(os.path.join(folder, "*.pdf")))
    if not pdf_files:
        return f"文件夹中没有 PDF 文件: {folder}"

    # 输出路径处理
    if not os.path.isabs(output) and not output.startswith(("~", ".", "/")):
        output = os.path.join(folder, output)
    output = os.path.expanduser(output)
    if not output.lower().endswith(".pdf"):
        output += ".pdf"

    try:
        subprocess.run(
            ["pdfunite", *pdf_files, output],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return "pdfunite 未安装，请运行: brew install poppler"
    except subprocess.CalledProcessError as e:
        return f"合并失败: {e.stderr.strip()}"

    filenames = [os.path.basename(f) for f in pdf_files]
    return (
        f"合并完成！\n"
        f"  文件数: {len(pdf_files)}\n"
        f"  输出到: {output}\n"
        f"  文件顺序: {', '.join(filenames)}"
    )
