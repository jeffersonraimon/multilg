import base64
import hashlib
import json
import os

from cryptography.fernet import Fernet, InvalidToken


class ConfigCipher:
    def __init__(self) -> None:
        secret = os.getenv("LG_SECRET_KEY", "")
        if len(secret) < 16:
            raise RuntimeError("LG_SECRET_KEY precisa ter pelo menos 16 caracteres")
        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
        self._fernet = Fernet(key)

    def encrypt(self, value: dict) -> bytes:
        payload = json.dumps(value, ensure_ascii=False).encode()
        return self._fernet.encrypt(payload)

    def decrypt(self, value: bytes) -> dict:
        try:
            return json.loads(self._fernet.decrypt(value).decode())
        except InvalidToken as exc:
            raise RuntimeError(
                "Não foi possível ler a configuração. A LG_SECRET_KEY foi alterada?"
            ) from exc

