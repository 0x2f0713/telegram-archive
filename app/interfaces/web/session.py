"""Signed browser sessions bound to the local Telegram account."""

from __future__ import annotations

import hashlib
import hmac
import time


class TelegramWebSession:
    """Create short-lived opaque cookies for one local Telegram identity."""

    cookie_name = "telegram_archiver_session"
    max_age = 60 * 60 * 24 * 30

    def __init__(self, secret: str) -> None:
        self._secret = secret.encode("utf-8")

    def issue(self, account_id: int, *, now: int | None = None) -> str:
        expires_at = (int(time.time()) if now is None else now) + self.max_age
        payload = f"{account_id}.{expires_at}".encode("ascii")
        signature = hmac.new(self._secret, payload, hashlib.sha256).hexdigest()
        return f"{account_id}.{expires_at}.{signature}"

    def valid(
        self,
        value: str | None,
        account_id: int | None,
        *,
        now: int | None = None,
    ) -> bool:
        if not value or account_id is None:
            return False
        parts = value.split(".")
        if len(parts) != 3:
            return False
        raw_account, raw_expiry, signature = parts
        if raw_account != str(account_id):
            return False
        try:
            expires_at = int(raw_expiry)
        except ValueError:
            return False
        if expires_at <= (int(time.time()) if now is None else now):
            return False
        payload = f"{raw_account}.{raw_expiry}".encode("ascii")
        expected = hmac.new(self._secret, payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
