"""環境変数（と .env）から設定を読む。"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_API_VERSION = "20260701"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class RakutenSettings:
    app_id: str
    access_key: str
    origin: str | None
    api_version: str


@dataclass(frozen=True)
class SheetsSettings:
    spreadsheet_id: str
    credentials_info: dict | None
    credentials_file: str | None


def _env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def load_env() -> None:
    # 既に設定済みの環境変数（GitHub Actions の Secrets 等）は上書きしない
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def rakuten_settings() -> RakutenSettings:
    app_id = _env("RAKUTEN_APP_ID")
    access_key = _env("RAKUTEN_ACCESS_KEY")
    missing = [
        name
        for name, value in (("RAKUTEN_APP_ID", app_id), ("RAKUTEN_ACCESS_KEY", access_key))
        if not value
    ]
    if missing:
        raise ConfigError(f"環境変数が未設定です: {', '.join(missing)}")
    return RakutenSettings(
        app_id=app_id,
        access_key=access_key,
        origin=_env("RAKUTEN_ORIGIN"),
        api_version=_env("RAKUTEN_API_VERSION") or DEFAULT_API_VERSION,
    )


def sheets_settings() -> SheetsSettings:
    raw_id = _env("SPREADSHEET_ID")
    if not raw_id:
        raise ConfigError("環境変数が未設定です: SPREADSHEET_ID")
    # URL を貼られても ID を取り出せるようにする
    match = re.search(r"/d/([A-Za-z0-9_-]+)", raw_id)
    spreadsheet_id = match.group(1) if match else raw_id

    info = None
    info_json = _env("GOOGLE_SERVICE_ACCOUNT_JSON")
    if info_json:
        try:
            info = json.loads(info_json)
        except json.JSONDecodeError:
            # 中身は秘密情報なのでエラーメッセージに含めない
            raise ConfigError("GOOGLE_SERVICE_ACCOUNT_JSON が JSON として読めません") from None
    credentials_file = _env("GOOGLE_APPLICATION_CREDENTIALS")
    if info is None and not credentials_file:
        raise ConfigError(
            "GOOGLE_SERVICE_ACCOUNT_JSON か GOOGLE_APPLICATION_CREDENTIALS のどちらかを設定してください"
        )
    return SheetsSettings(spreadsheet_id, info, credentials_file)
