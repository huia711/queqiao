"""Invite codes for shared deployments.

With ``QUEQIAO_ACCESS_MODE=invite`` anyone can read the library, but every action that spends model
quota or changes data needs an invite code, sent by the browser in the ``X-Queqiao-Key`` header.
Codes are stored as SHA-256 hashes in ``data/access.json``; each has a role (``admin`` or
``member``) and optional daily limits. The default mode ``open`` (desktop app, local use) allows
everything.

Manage codes with ``python -m app.access add NAME``, ``list`` and ``revoke ID``.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import secrets
import sys
import threading
import time
from dataclasses import dataclass

from .config import settings

HEADER = "x-queqiao-key"
ROLES = ("admin", "member")
DEFAULT_LIMITS = {"member": {"translate": 10, "ai": 200}, "admin": {"translate": 0, "ai": 0}}
_lock = threading.Lock()


class AccessDenied(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Identity:
    id: str
    name: str
    role: str
    limits: dict

    @property
    def admin(self) -> bool:
        return self.role == "admin"


OPEN_IDENTITY = Identity(id="local", name="本机", role="admin", limits={"translate": 0, "ai": 0})


def invite_mode() -> bool:
    return (settings.access_mode or "open").strip().lower() == "invite"


def _path():
    return settings.data_dir / "access.json"


def _usage_path():
    return settings.data_dir / "usage.json"


def _read(path, default):
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def _write(path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def load_codes() -> list[dict]:
    return [code for code in _read(_path(), {}).get("codes", []) if isinstance(code, dict)]


def _save_codes(codes: list[dict]) -> None:
    _write(_path(), {"codes": codes})


def hash_code(code: str) -> str:
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()


def create_code(name: str, role: str = "member", translate: int | None = None, ai: int | None = None) -> tuple[str, dict]:
    if role not in ROLES:
        raise ValueError("角色只能是 admin 或 member")
    name = " ".join((name or "").split())[:40]
    if not name:
        raise ValueError("请填写名称")
    code = "qq-" + secrets.token_urlsafe(18)
    limits = dict(DEFAULT_LIMITS[role])
    if translate is not None:
        limits["translate"] = max(0, int(translate))
    if ai is not None:
        limits["ai"] = max(0, int(ai))
    entry = {
        "id": secrets.token_hex(4),
        "name": name,
        "role": role,
        "hash": hash_code(code),
        "limits": limits,
        "created": time.time(),
        "disabled": False,
    }
    with _lock:
        codes = load_codes()
        codes.append(entry)
        _save_codes(codes)
    return code, public_entry(entry)


def revoke(code_id: str) -> bool:
    with _lock:
        codes = load_codes()
        found = False
        for entry in codes:
            if entry.get("id") == code_id:
                entry["disabled"] = True
                found = True
        if found:
            _save_codes(codes)
    return found


def public_entry(entry: dict) -> dict:
    today = usage_today().get(entry.get("id", ""), {})
    return {
        "id": entry.get("id"),
        "name": entry.get("name"),
        "role": entry.get("role"),
        "limits": entry.get("limits") or {},
        "created": entry.get("created"),
        "disabled": bool(entry.get("disabled")),
        "used_today": today,
    }


def identify(key: str | None) -> Identity | None:
    """Identity for a request; None means an anonymous visitor."""
    if not invite_mode():
        return OPEN_IDENTITY
    key = (key or "").strip()
    if not key or len(key) > 200:
        return None
    digest = hash_code(key)
    for entry in load_codes():
        if entry.get("disabled"):
            continue
        if hmac.compare_digest(str(entry.get("hash", "")), digest):
            role = entry.get("role") if entry.get("role") in ROLES else "member"
            return Identity(id=entry["id"], name=entry.get("name") or entry["id"], role=role, limits=entry.get("limits") or {})
    return None


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def usage_today() -> dict:
    data = _read(_usage_path(), {})
    return data.get(_today(), {}) if isinstance(data.get(_today()), dict) else {}


def require(identity: Identity | None, kind: str) -> Identity:
    """Check permission for ``kind`` (translate / ai / write / admin) and count quota use."""
    if identity is None:
        raise AccessDenied(401, "需要邀请码：此操作会消耗翻译额度或修改数据")
    if kind == "admin" and not identity.admin:
        raise AccessDenied(403, "只有管理员邀请码可以执行此操作")
    if kind in ("translate", "ai") and identity is not OPEN_IDENTITY:
        limit = int(identity.limits.get(kind) or 0)
        with _lock:
            data = _read(_usage_path(), {})
            today = _today()
            data = {today: data.get(today, {})}
            used = data[today].setdefault(identity.id, {})
            if limit and used.get(kind, 0) >= limit:
                label = "翻译" if kind == "translate" else "AI 研读"
                raise AccessDenied(429, f"今天的{label}额度已用完（{limit} 次），明天再试")
            used[kind] = used.get(kind, 0) + 1
            _write(_usage_path(), data)
    return identity


def describe(identity: Identity | None) -> dict:
    if not invite_mode():
        return {"mode": "open", "role": "admin", "name": "本机", "can_write": True}
    if identity is None:
        return {"mode": "invite", "role": "guest", "name": "访客", "can_write": False}
    if identity is OPEN_IDENTITY:
        return {"mode": "invite", "role": "admin", "name": "本机", "can_write": True, "local": True}
    used = usage_today().get(identity.id, {})
    return {
        "mode": "invite",
        "role": identity.role,
        "name": identity.name,
        "can_write": True,
        "limits": identity.limits,
        "used_today": used,
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.access", description="管理鹊桥邀请码")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="新建邀请码（只显示一次）")
    add.add_argument("name")
    add.add_argument("--role", choices=ROLES, default="member")
    add.add_argument("--translate", type=int, help="每天可提交的翻译数，0 表示不限")
    add.add_argument("--ai", type=int, help="每天可用的 AI 研读次数，0 表示不限")
    add.add_argument("--output", help="把邀请码写入这个文件（权限 600），而不是打印出来")
    sub.add_parser("list", help="列出邀请码")
    remove = sub.add_parser("revoke", help="停用邀请码")
    remove.add_argument("id")
    args = parser.parse_args()
    if args.command == "add":
        code, entry = create_code(args.name, args.role, args.translate, args.ai)
        if args.output:
            from pathlib import Path

            target = Path(args.output).expanduser()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(code + "\n", "utf-8")
            target.chmod(0o600)
            print(f"已创建 {entry['id']}（{entry['role']}），邀请码写入 {target}")
        else:
            print(code)
    elif args.command == "list":
        for entry in load_codes():
            state = "停用" if entry.get("disabled") else "有效"
            print(f"{entry.get('id')}  {entry.get('role'):6}  {state}  {entry.get('name')}  {entry.get('limits')}")
    elif args.command == "revoke":
        sys.exit(0 if revoke(args.id) else "未找到该邀请码")


if __name__ == "__main__":
    main()
