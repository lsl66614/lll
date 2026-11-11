"""启动入口：uv run python main.py

用文件入口而不是模块字符串，避免不同目录下路径解析出问题。
开发时可以改用：uv run fastapi dev main.py
"""

import uvicorn

from app import config
from app.main import app


def main() -> None:
    print(f"管理台: http://{config.HOST}:{config.PORT}/admin")
    if not config.GATEWAY_TOKEN and config.HOST not in ("127.0.0.1", "localhost", "::1"):
        print("警告：未设置 GATEWAY_TOKEN 且监听地址不是本机，局域网内任何人都能读写网关！")
    print("按 Ctrl+C 停止服务")
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="info")


if __name__ == "__main__":
    main()
