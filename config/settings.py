# -----------------------------------------------------------------------------
# Раздел: описание модуля
# -----------------------------------------------------------------------------
"""
Конфигурация проекта: настройки окружения, API-ключи и параметры логирования.
"""

# -----------------------------------------------------------------------------
# Раздел: импорты
# -----------------------------------------------------------------------------
import os
from typing import Any, Dict

from pydantic import BaseSettings, Field

# -----------------------------------------------------------------------------
# Раздел: класс настроек
# -----------------------------------------------------------------------------
class Settings(BaseSettings):
    """Основной класс конфигурации приложения."""

    # -----------------------------------------------------------------------------
    # Раздел: базовые параметры
    # -----------------------------------------------------------------------------
    bot_token: str = Field(..., env="BOT_TOKEN")
    db_path: str = Field("bot.db", env="DB_PATH")
    telegram_sms_channel_id: int = Field(0, env="TELEGRAM_SMS_CHANNEL_ID")

    # -----------------------------------------------------------------------------
    # Раздел: Fast-Flow API
    # -----------------------------------------------------------------------------
    ff_api_key: str = Field(..., env="FF_API_KEY")
    ff_api_secret: str = Field(..., env="FF_API_SECRET")

    # -----------------------------------------------------------------------------
    # Раздел: Paycore API
    # -----------------------------------------------------------------------------
    paycore_merchant_token: str = Field("", env="PAYCORE_MERCHANT_TOKEN")
    paycore_base_url: str = Field("https://paycore.pw", env="PAYCORE_BASE_URL")
    paycore_timeout_sec: int = Field(20, env="PAYCORE_TIMEOUT_SEC")

    # -----------------------------------------------------------------------------
    # Раздел: CrocoPay API
    # -----------------------------------------------------------------------------
    crocopay_client_id: str = Field("", env="CROCOPAY_CLIENT_ID")
    crocopay_client_secret: str = Field("", env="CROCOPAY_CLIENT_SECRET")
    crocopay_base_url: str = Field("https://crocopay.tech", env="CROCOPAY_BASE_URL")
    crocopay_timeout_sec: int = Field(20, env="CROCOPAY_TIMEOUT_SEC")
    crocopay_currency: str = Field("RUB", env="CROCOPAY_CURRENCY")
    crocopay_callback_url: str = Field(
        "https://api.akkulavidra.ru/crocopay/callback",
        env="CROCOPAY_CALLBACK_URL",
    )
    crocopay_success_url: str = Field(
        "https://api.akkulavidra.ru/crocopay/success",
        env="CROCOPAY_SUCCESS_URL",
    )
    crocopay_cancel_url: str = Field(
        "https://api.akkulavidra.ru/crocopay/cancel",
        env="CROCOPAY_CANCEL_URL",
    )

    # -----------------------------------------------------------------------------
    # Раздел: NirvanaPay API
    # -----------------------------------------------------------------------------
    nirvana_api_public: str = Field("", env="NIRVANA_API_PUBLIC")
    nirvana_api_private: str = Field("", env="NIRVANA_API_PRIVATE")
    nirvana_base_url: str = Field(
        "https://api.nirvanapay.pro",
        env="NIRVANA_BASE_URL",
    )
    nirvana_timeout_sec: int = Field(20, env="NIRVANA_TIMEOUT_SEC")
    nirvana_token: str = Field("СБП", env="NIRVANA_TOKEN")
    nirvana_currency: str = Field("RUB", env="NIRVANA_CURRENCY")
    nirvana_callback_url: str = Field(
        "https://api.akkulavidra.ru/nirvana/callback",
        env="NIRVANA_CALLBACK_URL",
    )

    # -----------------------------------------------------------------------------
    # Раздел: Akkula API
    # -----------------------------------------------------------------------------
    akkula_api_key: str = Field(..., env="AKKULA_API_KEY")
    akkula_base_url: str = Field(
        "https://akkula.kg/api/partner/v1",
        env="AKKULA_BASE_URL",
    )
    akkula_timeout_sec: int = Field(20, env="AKKULA_TIMEOUT_SEC")

    akkula_api_secret: str = Field("", env="AKKULA_API_SECRET")
    akkula_webhook_secret: str = Field("", env="AKKULA_WEBHOOK_SECRET")

    akkula_webhook_host: str = Field("0.0.0.0", env="AKKULA_WEBHOOK_HOST")
    akkula_webhook_port: int = Field(8082, env="AKKULA_WEBHOOK_PORT")

    # -----------------------------------------------------------------------------
    # Раздел: Bybit API
    # -----------------------------------------------------------------------------
    bybit_api_key: str = Field(..., env="BYBIT_API_KEY")
    bybit_api_secret: str = Field(..., env="BYBIT_API_SECRET")
    bybit_base_url: str = Field("https://api.bybit.com", env="BYBIT_BASE_URL")

    # -----------------------------------------------------------------------------
    # Раздел: Binance API
    # -----------------------------------------------------------------------------
    binance_api_key: str = Field(..., env="BINANCE_API_KEY")
    binance_api_secret: str = Field(..., env="BINANCE_API_SECRET")
    binance_base_url: str = Field("https://api.binance.com", env="BINANCE_BASE_URL")
    binance_proxy: str = Field("", env="BINANCE_PROXY")

    # -----------------------------------------------------------------------------
    # Раздел: системные параметры
    # -----------------------------------------------------------------------------
    operator_id: int = Field(6216500555, env="OPERATOR_ID")
    log_dir: str = Field("logs", env="LOG_DIR")

    # -----------------------------------------------------------------------------
    # Раздел: логирование
    # -----------------------------------------------------------------------------
    logging_config: Dict[str, Any] = Field(
        default_factory=lambda: {
            "version": 1,
            "formatters": {
                "default": {
                    "format": "[%(asctime)s] %(levelname)s:%(name)s: %(message)s"
                }
            },
            "handlers": {
                "file": {
                    "class": "logging.FileHandler",
                    "filename": os.path.join(
                        os.getenv("LOG_DIR", "logs"),
                        "bot.log",
                    ),
                    "formatter": "default",
                },
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                },
            },
            "root": {
                "handlers": ["console", "file"],
                "level": "INFO",
            },
        }
    )

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

# -----------------------------------------------------------------------------
# Раздел: инициализация и экспорт констант
# -----------------------------------------------------------------------------
settings = Settings()  # type: ignore
os.makedirs(settings.log_dir, exist_ok=True)

BOT_TOKEN = settings.bot_token
DB_PATH = settings.db_path

FF_API_KEY = settings.ff_api_key
FF_API_SECRET = settings.ff_api_secret

PAYCORE_MERCHANT_TOKEN = settings.paycore_merchant_token
PAYCORE_BASE_URL = settings.paycore_base_url
PAYCORE_TIMEOUT_SEC = settings.paycore_timeout_sec

CROCOPAY_CLIENT_ID = settings.crocopay_client_id
CROCOPAY_CLIENT_SECRET = settings.crocopay_client_secret
CROCOPAY_BASE_URL = settings.crocopay_base_url
CROCOPAY_TIMEOUT_SEC = settings.crocopay_timeout_sec
CROCOPAY_CURRENCY = settings.crocopay_currency
CROCOPAY_CALLBACK_URL = settings.crocopay_callback_url
CROCOPAY_SUCCESS_URL = settings.crocopay_success_url
CROCOPAY_CANCEL_URL = settings.crocopay_cancel_url

NIRVANA_API_PUBLIC = settings.nirvana_api_public
NIRVANA_API_PRIVATE = settings.nirvana_api_private
NIRVANA_BASE_URL = settings.nirvana_base_url
NIRVANA_TIMEOUT_SEC = settings.nirvana_timeout_sec
NIRVANA_TOKEN = settings.nirvana_token
NIRVANA_CURRENCY = settings.nirvana_currency
NIRVANA_CALLBACK_URL = settings.nirvana_callback_url

AKKULA_API_KEY = settings.akkula_api_key
AKKULA_BASE_URL = settings.akkula_base_url
AKKULA_TIMEOUT_SEC = settings.akkula_timeout_sec
AKKULA_API_SECRET = settings.akkula_api_secret
AKKULA_WEBHOOK_SECRET = settings.akkula_webhook_secret
AKKULA_WEBHOOK_HOST = settings.akkula_webhook_host
AKKULA_WEBHOOK_PORT = settings.akkula_webhook_port

BYBIT_API_KEY = settings.bybit_api_key
BYBIT_API_SECRET = settings.bybit_api_secret
BYBIT_BASE_URL = settings.bybit_base_url

BINANCE_API_KEY = settings.binance_api_key
BINANCE_API_SECRET = settings.binance_api_secret
BINANCE_BASE_URL = settings.binance_base_url
BINANCE_PROXY = settings.binance_proxy

OPERATOR_ID = settings.operator_id
TELEGRAM_SMS_CHANNEL_ID = settings.telegram_sms_channel_id