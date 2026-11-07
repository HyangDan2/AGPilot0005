"""Builtin System Tools: system.echo, system.get_datetime."""
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Dict

async def echo(message: str) -> Dict[str, Any]:
    """Echoes back the received message."""
    return {"message": message}

async def get_datetime(timezone: str = "Asia/Seoul") -> Dict[str, Any]:
    """Returns the current server date, time and ISO-8601 string in the requested timezone."""
    try:
        tz = ZoneInfo(timezone)
    except Exception:
        tz = ZoneInfo("Asia/Seoul")
    
    now = datetime.now(tz)
    return {
        "timezone": tz.key,
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "iso": now.isoformat(),
        "weekday": now.strftime("%A"),
    }
