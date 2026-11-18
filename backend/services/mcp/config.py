from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]  # services/mcp/ → backend/

MCP_SERVERS = [
    {
        "name": "devsentinel",
        "command": str(BACKEND_DIR / ".venv" / "bin" / "python"),
        "args": [str(BACKEND_DIR / "mcp_server.py")],
    },
    {
        "name": "filesystem",
        "command": "/Users/cxd/.nvm/versions/node/v22.22.1/bin/npx",
        "args": [
            "-y",
            "@modelcontextprotocol/server-filesystem",
            "/Users/cxd/code",                        # 授权它能读的根目录
        ],
    },
]