"""Ahem Demo 安全預檢：只輸出通過／阻擋原因，不輸出任何秘密值。"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import socket
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from .security import KeychainKEK


@dataclasses.dataclass(frozen=True)
class Check:
    name: str
    status: str
    message: str


def _token_checks(env: dict[str, str]) -> list[Check]:
    viewer = env.get("AHEM_VIEWER_TOKEN", "")
    operator = env.get("AHEM_OPERATOR_TOKEN", "")
    present = len(viewer) >= 32 and len(operator) >= 32
    distinct = present and viewer != operator
    return [
        Check("access_tokens", "pass" if present else "fail",
              "Viewer／Operator Token 已設定且長度合格" if present
              else "需要兩個至少 32 字元的 Token"),
        Check("role_separation", "pass" if distinct else "fail",
              "Viewer／Operator Token 已分離" if distinct
              else "兩個角色必須使用不同 Token"),
    ]


def _storage_check(directory: Path) -> Check:
    if not directory.exists():
        return Check("private_storage", "warn", f"{directory} 尚未建立；啟動時必須建立為 0700")
    mode = stat.S_IMODE(directory.stat().st_mode)
    return Check("private_storage", "pass" if mode == 0o700 else "fail",
                 f"資料目錄權限為 {mode:04o}" + ("" if mode == 0o700 else "，必須修正為 0700"))


def _port_check(host: str, port: int) -> Check:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
    except OSError:
        return Check("listen_port", "fail", f"{host}:{port} 已被占用或無法綁定")
    finally:
        sock.close()
    return Check("listen_port", "pass", f"{host}:{port} 可安全啟動")


def run_checks(*, mode: str, host: str, port: int, directory: Path,
               env: dict[str, str] | None = None,
               keychain_loader: Callable[[], bytes] | None = None) -> list[Check]:
    env = dict(os.environ if env is None else env)
    checks = _token_checks(env)
    checks.append(Check(
        "secure_storage",
        "pass" if env.get("AHEM_SECURE_STORAGE") == "1" else "fail",
        "已啟用加密儲存" if env.get("AHEM_SECURE_STORAGE") == "1"
        else "AHEM_SECURE_STORAGE 必須設為 1",
    ))
    try:
        kek = (keychain_loader or KeychainKEK().load)()
        keychain_ok = len(kek) == 32
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        keychain_ok = False
    checks.append(Check(
        "key_encryption_key", "pass" if keychain_ok else "fail",
        "Keychain KEK 可用且長度合格" if keychain_ok
        else "找不到可用的 32-byte Keychain KEK",
    ))
    checks.append(Check(
        "external_search",
        "pass" if env.get("AHEM_DISABLE_WEB_SEARCH") == "1" else "warn",
        "Demo 已停用外部網路搜尋" if env.get("AHEM_DISABLE_WEB_SEARCH") == "1"
        else "建議 Demo 設定 AHEM_DISABLE_WEB_SEARCH=1",
    ))
    origins = tuple(filter(None, env.get("AHEM_TRUSTED_ORIGINS", "").split(",")))
    if mode == "local":
        checks.append(Check("network_boundary", "pass" if host == "127.0.0.1" else "fail",
                            "僅監聽本機 loopback" if host == "127.0.0.1"
                            else "local 模式只能使用 127.0.0.1"))
    else:
        https_only = bool(origins) and all(origin.startswith("https://") for origin in origins)
        cookie_secure = env.get("AHEM_COOKIE_SECURE") == "1"
        ok = https_only and cookie_secure
        checks.append(Check(
            "network_boundary", "pass" if ok else "fail",
            "LAN 模式已限制 HTTPS Origin 並強制 Secure Cookie" if ok
            else "LAN 模式需要 HTTPS trusted origin 與 AHEM_COOKIE_SECURE=1",
        ))
    checks.extend([_storage_check(directory), _port_check(host, port)])
    return checks


def summary(checks: list[Check]) -> dict[str, object]:
    counts = {status: sum(check.status == status for check in checks)
              for status in ("pass", "warn", "fail")}
    return {
        "ready": counts["fail"] == 0,
        "counts": counts,
        "checks": [dataclasses.asdict(check) for check in checks],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="執行 Ahem Demo 安全預檢（不輸出秘密）")
    parser.add_argument("--mode", choices=("local", "lan"), default="local")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--directory", type=Path, default=Path("meetings"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = summary(run_checks(mode=args.mode, host=args.host, port=args.port,
                                directory=args.directory))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for check in result["checks"]:
            print(f"[{check['status'].upper():4}] {check['name']}: {check['message']}")
        print("READY" if result["ready"] else "BLOCKED")
    sys.exit(0 if result["ready"] else 2)


if __name__ == "__main__":
    main()
