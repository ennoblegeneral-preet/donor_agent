"""Optional Redis cache for expensive external reads."""

import hashlib
import json
import os
import threading
from dotenv import load_dotenv

load_dotenv()

try:
    import redis
except ImportError:
    redis = None

_client = None
_client_lock = threading.Lock()
_warned = False


def _get_client():
    global _client, _warned
    if redis is None or os.getenv("REDIS_ENABLED", "true").lower() in {"0", "false", "no", "off"}:
        if redis is None and not _warned:
            print("[Redis] redis package is not installed; cache disabled.")
            _warned = True
        return None
    if _client is None:
        with _client_lock:
            if _client is None:
                try:
                    candidate = redis.Redis.from_url(
                        os.getenv("REDIS_URL", "redis://localhost:6379/0"),
                        decode_responses=True,
                        socket_connect_timeout=1.5,
                        socket_timeout=2.5,
                    )
                    candidate.ping()
                    _client = candidate
                    print("[Redis] Cache connected.")
                except Exception as exc:
                    if not _warned:
                        print(f"[Redis] Cache unavailable; using live data path: {exc}")
                        _warned = True
                    return None
    return _client


def make_key(namespace: str, *parts) -> str:
    material = "\x1f".join(str(part or "") for part in parts)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"donoriq:{namespace}:{digest}"


def get_json(key: str):
    client = _get_client()
    if client is None:
        return None
    try:
        value = client.get(key)
        return json.loads(value) if value else None
    except Exception as exc:
        print(f"[Redis] Read failed; continuing without cache: {exc}")
        return None


def set_json(key: str, value, ttl_seconds: int = None) -> bool:
    client = _get_client()
    if client is None:
        return False
    try:
        ttl = int(ttl_seconds or os.getenv("REDIS_CACHE_TTL", "86400"))
        client.setex(key, max(ttl, 1), json.dumps(value, ensure_ascii=False, default=str))
        return True
    except Exception as exc:
        print(f"[Redis] Write failed; continuing without cache: {exc}")
        return False
