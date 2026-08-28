import hmac
import hashlib
import time
from urllib.parse import parse_qsl

def validate_init_data(init_data: str, bot_token: str) -> dict | None:
    """
    Validates Telegram WebApp initData per AGENTS:35.
    Returns parsed data dict if valid, else None.
    Never trust initDataUnsafe.
    """
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        hash_received = parsed.pop("hash", None)
        if not hash_received:
            return None
        # Reject stale and implausibly future Telegram init data.
        auth_date = int(parsed.get("auth_date", "0"))
        age = time.time() - auth_date
        if age > 86400 or age < -60:
            return None
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc_hash, hash_received):
            return None
        return parsed
    except:
        return None

def parse_user_from_init_data(parsed: dict) -> dict | None:
    import json
    user_json = parsed.get("user")
    if not user_json:
        return None
    try:
        return json.loads(user_json)
    except:
        return None
