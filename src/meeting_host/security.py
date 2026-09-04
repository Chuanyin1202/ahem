"""Ahem 的資料保護原語：同意閘門、安全寫檔、封套加密與內容最少化稽核。"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import os
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


@dataclasses.dataclass(frozen=True)
class ConsentPolicy:
    """未取得同意時，禁止把個資送往外部服務。"""

    granted: bool
    privacy_mode: str = "strict"

    def require(self, service: str, *, personal_data: bool = True) -> None:
        if personal_data and self.privacy_mode == "strict" and not self.granted:
            raise PermissionError(f"尚未取得參與者同意，不得將個資送往 {service}")


def prepare_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def secure_write_text(path: Path, text: str) -> Path:
    """以 0600 暫存檔原子取代目標，避免半寫入或沿用過寬權限。"""
    path = Path(path)
    prepare_private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return path


class KeychainKEK:
    """從 macOS Keychain 讀取 32-byte base64 KEK；不接受 .env 中的主金鑰。"""

    service = "ahem.envelope-kek"

    def load(self) -> bytes:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", self.service, "-w"],
            check=True, capture_output=True, text=True,
        )
        raw = base64.b64decode(result.stdout.strip(), validate=True)
        if len(raw) != 32:
            raise ValueError("Ahem KEK 必須是 32 bytes")
        return raw


class FileKEK:
    """Linux 從 systemd credential 或 container secret 檔案讀取 KEK。"""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> bytes:
        if not self.path.is_absolute():
            raise ValueError("AHEM_KEK_FILE 必須是絕對路徑")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags)
        except OSError as exc:
            raise ValueError("KEK 路徑必須是存在的一般檔案，不可為符號連結") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("KEK 路徑必須是一般檔案")
            mode = stat.S_IMODE(info.st_mode)
            if mode & 0o077:
                raise PermissionError(f"KEK 檔案權限過寬：{mode:04o}，必須為 0600 或更嚴格")
            allowed_owners = {os.geteuid(), 0}
            if info.st_uid not in allowed_owners:
                raise PermissionError("KEK 檔案必須由服務帳號或 root 擁有")
            with os.fdopen(fd, "r", encoding="ascii") as handle:
                fd = -1
                encoded = handle.read().strip()
        finally:
            if fd >= 0:
                os.close(fd)
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) != 32:
            raise ValueError("Ahem KEK 必須是 32 bytes")
        return raw


def load_kek(env: dict[str, str] | None = None) -> bytes:
    """依平台選擇 KEK provider；測試由呼叫端明確注入 loader。"""
    env = dict(os.environ if env is None else env)
    configured = env.get("AHEM_KEK_FILE", "").strip()
    credential_dir = env.get("CREDENTIALS_DIRECTORY", "").strip()
    if configured:
        return FileKEK(Path(configured)).load()
    if credential_dir:
        return FileKEK(Path(credential_dir) / "ahem-kek").load()
    if sys.platform == "darwin":
        return KeychainKEK().load()
    container_secret = Path("/run/secrets/ahem_kek")
    if container_secret.is_file():
        return FileKEK(container_secret).load()
    raise RuntimeError(
        "Linux 需以 AHEM_KEK_FILE、systemd LoadCredential=ahem-kek 或 "
        "/run/secrets/ahem_kek 提供 KEK"
    )


class EnvelopeStore:
    """AES-256-GCM 封套加密；每個物件使用獨立 DEK 與唯一 Nonce。"""

    def __init__(self, kek: bytes):
        if len(kek) != 32:
            raise ValueError("KEK 必須是 32 bytes")
        self._kek = kek

    @staticmethod
    def _aad(metadata: dict[str, str]) -> bytes:
        keys = ("schema_version", "meeting_id", "artifact_type", "created_at", "privacy_mode")
        return "|".join(metadata[key] for key in keys).encode("utf-8")

    def encrypt_text(self, text: str, *, meeting_id: str, artifact_type: str,
                     privacy_mode: str = "strict") -> bytes:
        metadata = {
            "schema_version": "1",
            "meeting_id": meeting_id,
            "artifact_type": artifact_type,
            "created_at": str(int(time.time())),
            "privacy_mode": privacy_mode,
        }
        aad = self._aad(metadata)
        dek = AESGCM.generate_key(bit_length=256)
        data_nonce, wrap_nonce = secrets.token_bytes(12), secrets.token_bytes(12)
        payload = {
            **metadata,
            "algorithm": "AES-256-GCM",
            "nonce": base64.b64encode(data_nonce).decode(),
            "ciphertext": base64.b64encode(AESGCM(dek).encrypt(data_nonce, text.encode(), aad)).decode(),
            "wrap_nonce": base64.b64encode(wrap_nonce).decode(),
            "wrapped_dek": base64.b64encode(AESGCM(self._kek).encrypt(wrap_nonce, dek, aad)).decode(),
        }
        return (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode()

    def decrypt_text(self, blob: bytes, *, meeting_id: str, artifact_type: str,
                     purpose: str, operator: bool) -> str:
        if not operator or not purpose.strip():
            raise PermissionError("解密需要 Operator 身分與明確用途")
        payload = json.loads(blob)
        if payload["meeting_id"] != meeting_id or payload["artifact_type"] != artifact_type:
            raise ValueError("加密資料與要求的會議或檔案類型不符")
        metadata = {key: payload[key] for key in (
            "schema_version", "meeting_id", "artifact_type", "created_at", "privacy_mode")}
        aad = self._aad(metadata)
        dek = AESGCM(self._kek).decrypt(
            base64.b64decode(payload["wrap_nonce"]), base64.b64decode(payload["wrapped_dek"]), aad)
        plaintext = AESGCM(dek).decrypt(
            base64.b64decode(payload["nonce"]), base64.b64decode(payload["ciphertext"]), aad)
        return plaintext.decode()


def write_protected_text(path: Path, text: str, *, artifact_type: str) -> Path:
    """AHEM_SECURE_STORAGE=1 時不落明文；開發模式仍強制 0600。"""
    path = Path(path)
    if os.environ.get("AHEM_SECURE_STORAGE") != "1":
        return secure_write_text(path, text)
    meeting_id = os.environ.get("AHEM_MEETING_ID") or path.name.split(".")[0]
    store = EnvelopeStore(load_kek())
    encrypted_path = path.with_name(path.name + ".ahem")
    blob = store.encrypt_text(text, meeting_id=meeting_id, artifact_type=artifact_type)
    return secure_write_text(encrypted_path, blob.decode())


def audit_record(action: str, *, actor: str, meeting_id: str, purpose: str,
                 outcome: str) -> dict[str, str | int]:
    """稽核只存中繼資料；actor 雜湊化，不含逐字稿或金鑰。"""
    return {
        "at": int(time.time()),
        "action": action,
        "actor_ref": hashlib.sha256(actor.encode()).hexdigest()[:16],
        "meeting_id": meeting_id,
        "purpose": purpose,
        "outcome": outcome,
    }


def redact_event_for_viewer(event: dict) -> dict:
    """Viewer 只取得運作狀態；對所有已知嵌套內容做去識別。"""
    event = json.loads(json.dumps(event))
    data = event.get("data") if isinstance(event, dict) else None
    if not isinstance(data, dict):
        return event
    for key in ("text", "host_md", "minutes_md", "participant_md", "speaker", "target", "topic"):
        if key in data:
            data[key] = "[已隱去]"
    if "participants" in data:
        data["participants"] = [f"P{i + 1:02d}" for i, _ in enumerate(data["participants"])]
    for key in ("host_path", "minutes_path", "log_path", "events_path"):
        data.pop(key, None)
    kind = event.get("kind")
    if kind == "share":
        event["data"] = {f"P{i + 1:02d}": value for i, value in enumerate(data.values())}
    elif kind == "glossary":
        event["data"] = {"term": "[已隱去]", "explained": bool(data.get("explained"))}
    elif kind == "slow_score":
        for key in ("utterance", "reason", "pros", "cons"):
            if key in data:
                data[key] = "[已隱去]"
    elif kind == "phase_suggestion" and "reason" in data:
        data["reason"] = "[已隱去]"
    return event
