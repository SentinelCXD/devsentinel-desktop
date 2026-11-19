import os
import shutil
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]  # services/mcp/ → backend/


def _npx() -> str:
    """解析 npx 可执行文件路径，不硬编码。

    优先级：MCP_NPX 环境变量 > PATH 查找（shutil.which）> 裸命令名
    （交给子进程环境解析，找不到时报错信息也更直观）。
    """
    return os.environ.get("MCP_NPX") or shutil.which("npx") or "npx"


def _fs_root() -> str:
    """filesystem server 的授权根目录：MCP_FS_ROOT 环境变量可覆盖，
    默认用户家目录（覆盖任意代码目录布局；注意含 .ssh 等敏感目录，
    如需收窄请设 MCP_FS_ROOT）。"""
    return os.environ.get("MCP_FS_ROOT") or str(Path.home())


MCP_SERVERS = [
    {
        "name": "devsentinel",
        "command": str(BACKEND_DIR / ".venv" / "bin" / "python"),
        "args": [str(BACKEND_DIR / "mcp_server.py")],
    },
    {
        "name": "filesystem",
        "command": _npx(),
        "args": [
            "-y",
            "@modelcontextprotocol/server-filesystem",
            _fs_root(),                                 # 授权它能读的根目录
        ],
    },
]