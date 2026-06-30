"""
手机文件推送模块
通过 MTP（USB 文件传输）将表情包复制到手机存储
使用 PowerShell COM 接口实现可靠的 MTP 文件复制
"""
import logging
import subprocess
import time
from pathlib import Path

import config

logger = logging.getLogger(__name__)

REMOTE_STICKER_DIR = "DCIM/stickers"


def _run_ps(script: str, timeout: int = 60) -> str:
    """执行 PowerShell 脚本并返回输出"""
    result = subprocess.run(
        ["powershell", "-Command", script],
        capture_output=True, timeout=timeout,
    )
    # Windows 中文环境 PowerShell 输出可能是 GBK
    for enc in ("utf-8", "gbk", "gb2312"):
        try:
            stdout = result.stdout.decode(enc)
            stderr = result.stderr.decode(enc) if result.stderr else ""
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        stdout = result.stdout.decode("latin-1")
        stderr = result.stderr.decode("latin-1") if result.stderr else ""

    if result.returncode != 0:
        raise RuntimeError(f"PowerShell 执行失败: {stderr}")
    return stdout


def find_phone() -> str:
    """查找已连接的 MTP 手机，返回设备名称"""
    ps = r'''
    $shell = New-Object -ComObject Shell.Application
    $pc = $shell.NameSpace(0x11)
    foreach ($item in $pc.Items()) {
        $path = $item.Path
        if ($path -notmatch '^[A-Z]:' -and $path -notmatch '::\\Volume') {
            Write-Output $item.Name
        }
    }
    '''
    output = _run_ps(ps).strip()
    if not output:
        raise RuntimeError(
            "未检测到手机，请确认：\n"
            "1. 手机已通过 USB 连接电脑\n"
            "2. 手机上选择了「文件传输」模式\n"
            "3. 手机上已授权此电脑访问文件"
        )
    return output


def _build_copy_script(local_paths: list[Path], remote_dir: str) -> str:
    """生成 PowerShell 复制脚本"""
    # 转义路径中的特殊字符（用反引号转义 PowerShell 特殊字符）
    escaped_src = str(local_paths[0].parent.resolve()).replace("'", "''")
    file_names = [p.name.replace("'", "''") for p in local_paths]
    names_str = "','".join(file_names)

    # 远程目录层级
    parts = remote_dir.split("/")

    return f'''
$shell = New-Object -ComObject Shell.Application
$pc = $shell.NameSpace(0x11)

# 找手机
$phone = $null
foreach ($item in $pc.Items()) {{
    if ($item.Path -notmatch '^[A-Z]:' -and $item.Path -notmatch '::\\\\Volume') {{
        $phone = $item
        break
    }}
}}

# 内部存储
$storage = $null
foreach ($child in $phone.GetFolder.Items()) {{
    if ($child.Name -like '*内部*' -or $child.Name -like '*Internal*') {{
        $storage = $child
        break
    }}
}}

# 逐级导航
$current = $storage
foreach ($part in @({",".join(f'"{p}"' for p in parts)})) {{
    $found = $null
    foreach ($child in $current.GetFolder.Items()) {{
        if ($child.Name -eq $part) {{
            $found = $child
            break
        }}
    }}
    if (-not $found) {{
        $current.GetFolder.NewFolder($part)
        Start-Sleep -Milliseconds 500
        foreach ($child in $current.GetFolder.Items()) {{
            if ($child.Name -eq $part) {{
                $found = $child
                break
            }}
        }}
    }}
    $current = $found
}}

# 源文件夹
$srcFolder = $shell.Namespace("{escaped_src}")

# 逐个复制
$names = @('{names_str}')
$total = $names.Count
$success = 0
foreach ($name in $names) {{
    $item = $srcFolder.ParseName($name)
    if ($item) {{
        $current.GetFolder.CopyHere($item, 16)
        Start-Sleep -Milliseconds 500
        $success++
        Write-Host "[$success/$total] $name"
    }} else {{
        Write-Host "SKIP: $name"
    }}
}}
Write-Host "DONE: $success/$total"
'''


def push_files_mtp(local_paths: list[Path], remote_dir: str, on_progress=None) -> list[str]:
    """通过 MTP 复制文件到手机"""
    emit = on_progress or (lambda msg: logger.info(msg))

    emit(f"正在查找手机...")
    phone_name = find_phone()
    emit(f"找到设备: {phone_name}")

    emit(f"正在复制 {len(local_paths)} 个文件到 {remote_dir}...")
    script = _build_copy_script(local_paths, remote_dir)
    output = _run_ps(script, timeout=max(120, len(local_paths) * 5))

    # 解析输出
    pushed = []
    for line in output.strip().split("\n"):
        line = line.strip()
        if line.startswith("["):
            # [1/10] filename.gif
            emit(line)
            parts = line.split("] ", 1)
            if len(parts) > 1:
                pushed.append(f"{remote_dir}/{parts[1]}")
        elif line.startswith("DONE:"):
            emit(line)

    return pushed


def push_stickers_to_phone(
    paths: list[Path],
    remote_dir: str | None = None,
    on_progress=None,
) -> int:
    """复制表情包到手机，返回成功数量"""
    emit = on_progress or (lambda msg: logger.info(msg))

    target = remote_dir or REMOTE_STICKER_DIR
    pushed = push_files_mtp(paths, target, on_progress=emit)
    emit(f"成功复制 {len(pushed)}/{len(paths)} 个表情包到手机")

    if pushed:
        emit(f"手机路径: {target}")
        emit("请在手机微信中：打开聊天 -> 点击表情 -> 红心标签 -> 添加单个表情 -> 从相册选择")

    return len(pushed)
