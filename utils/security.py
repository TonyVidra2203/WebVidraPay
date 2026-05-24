# -----------------------------------------------------------------------------
# Раздел: Импорты
# -----------------------------------------------------------------------------

import hashlib
import os
from typing import Optional


# -----------------------------------------------------------------------------
# Раздел: Хэширование и проверка PIN
# -----------------------------------------------------------------------------

def hash_pin(pin: str, salt: Optional[bytes] = None) -> str:
    """
    Хэшировать PIN (4–6 цифр): сохраняется в виде "salt_hex:sha256(salt+pin)".
    """
    if salt is None:
        salt = os.urandom(16)
    digest = hashlib.sha256(salt + pin.encode("utf-8")).hexdigest()
    return f"{salt.hex()}:{digest}"


def verify_pin(pin: str, stored: str) -> bool:
    """
    Проверить PIN: вычислить sha256(salt+pin) и сравнить с сохранённым значением.
    """
    salt_hex, stored_hash = stored.split(":", 1)
    salt = bytes.fromhex(salt_hex)
    computed_hash = hashlib.sha256(salt + pin.encode("utf-8")).hexdigest()
    return computed_hash == stored_hash
