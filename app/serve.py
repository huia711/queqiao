"""Run Queqiao on a loopback TCP port and, optionally, a Unix socket for a reverse tunnel.

One process serves both, so jobs and caches are shared:

- ``127.0.0.1:<port>``: browsers on this machine; treated as the owner (admin, no invite code).
- ``--uds <path>``: target of ``ssh -R <remote_port>:<path>`` from the public gateway. Requests on
  this socket have no client address, so they never get local privileges and must carry the
  gateway secret and an invite code like any other public request.

Usage: ``python -m app.serve --port 8686 --uds ~/Library/Application\\ Support/Queqiao/tunnel.sock``
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import stat
from pathlib import Path

import uvicorn


def unix_socket(path: Path) -> socket.socket:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise SystemExit(f"{path} 已存在且不是 socket")
        path.unlink()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        sock.bind(str(path))
    finally:
        os.umask(old)
    sock.listen(128)
    return sock


def tcp_socket(host: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(128)
    return sock


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.serve")
    parser.add_argument("--host", default=os.environ.get("QUEQIAO_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("QUEQIAO_PORT", "8686")))
    parser.add_argument("--uds", default=os.environ.get("QUEQIAO_UDS", ""), help="Unix socket for a reverse tunnel")
    parser.add_argument("--log-level", default="warning")
    args = parser.parse_args()
    sockets = [tcp_socket(args.host, args.port)]
    if args.uds:
        sockets.append(unix_socket(Path(args.uds).expanduser()))
    config = uvicorn.Config("app.main:asgi", log_level=args.log_level, proxy_headers=False, timeout_keep_alive=30)
    asyncio.run(uvicorn.Server(config).serve(sockets=sockets))


if __name__ == "__main__":
    main()
