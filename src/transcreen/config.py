"""User configuration persisted as JSON under %APPDATA%/Transcreen."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

logger = logging.getLogger(__name__)


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "Transcreen"


def _config_path() -> Path:
    return config_dir() / "config.json"


@dataclass
class Config:
    target_lang: str = "tr"
    ocr_lang: str = "auto"  # BCP-47 tag ("en-US") or "auto"
    idle_poll: float = 0.012  # sleep between unchanged-frame checks (s)
    min_text_height: int = 10  # ignore OCR lines shorter than this (px)
    use_local_mt: bool = True  # offline en->tr prefill tier (kill-switch)

    @classmethod
    def load(cls) -> Config:
        path = _config_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, json.JSONDecodeError):
            logger.warning("config unreadable, using defaults: %s", path, exc_info=True)
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self) -> None:
        path = _config_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            logger.warning("could not save config to %s", path, exc_info=True)
