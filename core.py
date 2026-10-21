"""Deterministic helpers shared by the bot and processing pipeline."""
import ipaddress
import re
from urllib.parse import urlparse


def is_http_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            return False
        host = parsed.hostname.lower().rstrip(".")
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            return False
        try:
            address = ipaddress.ip_address(host)
            if not address.is_global:
                return False
        except ValueError:
            pass
        return True
    except (ValueError, TypeError):
        return False


def score_candidate(text: str, speech_coverage: float, duration_s: float) -> int:
    low = text.lower()
    score = 38.0
    hooks = [
        "why", "how", "secret", "never", "actually", "mistake", "because", "but",
        "suddenly", "imagine", "the truth", "you need to", "here is", "here’s",
        "what happened", "почему", "как", "секрет", "никогда", "ошибка",
        "на самом деле", "но", "представь", "вот почему",
    ]
    score += min(22, sum(4 for hook in hooks if hook in low))
    words = re.findall(r"\w+", text)
    unique_ratio = len(set(word.lower() for word in words)) / max(1, len(words))
    score += min(10, unique_ratio * 10)
    score += min(12, max(0, speech_coverage) * 12)
    filler = len(re.findall(r"\b(um|uh|erm|ээ|эм|ну|короче)\b", low))
    score -= min(12, filler * 2)
    if text.rstrip().endswith(("?", "!", ".", "…")):
        score += 5
    if len(words) < 18:
        score -= 8
    if len(words) > 110:
        score -= 6
    if duration_s < 12:
        score -= 6
    return max(1, min(100, round(score)))


def ass_time(seconds: float) -> str:
    centiseconds = int(max(0, seconds) * 100)
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    seconds_part, centiseconds_part = divmod(remainder, 100)
    return f"{hours}:{minutes:02}:{seconds_part:02}.{centiseconds_part:02}"
