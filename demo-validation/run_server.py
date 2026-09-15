"""启动 Web 服务。

用法：
    python run_server.py [--host 127.0.0.1] [--port 8000]

**为什么不用 `uvicorn app.server:app`？**
    uvicorn 在 Windows 上会显式选用 `ProactorEventLoop`
    （见 uvicorn/loops/asyncio.py: `asyncio_loop_factory`），
    而 psycopg 的异步连接在 Windows 上只能跑 `SelectorEventLoop`。
    这里自己拿循环直接 `serve()`，绕开 uvicorn 的循环工厂。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

if sys.platform == "win32":  # 必须在建立事件循环之前设置
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]

import uvicorn  # noqa: E402

from app.server import app  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="多 Agent 协作一致性演示 · Web 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
    server = uvicorn.Server(config)

    print("=" * 62)
    print("  多 Agent 协作一致性演示")
    print(f"  打开浏览器：http://{args.host}:{args.port}")
    print("=" * 62)

    try:
        # 直接驱动 serve()，不经过 Server.run() 的 loop_factory
        asyncio.run(server.serve())
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
