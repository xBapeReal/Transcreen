"""Generate Transcreen.py: the whole app as ONE portable Python file.

The generated file embeds every src/transcreen module (zlib+base64) and
serves them through an in-memory import loader, so it runs identically from
any folder or any Windows machine. On a machine without the dependencies it
bootstraps its own venv under %LOCALAPPDATA%\\Transcreen (never next to
the file: synced or non-ASCII folders such as OneDrive are hostile to
virtual environments) and relaunches itself from there.

Usage: uv run python tools/build_onefile.py [--out PATH]   (default: dist/Transcreen.py)
The file is a build artifact: it is not committed, regenerate it after every
source change (or attach it to a GitHub release).
"""

from __future__ import annotations

import argparse
import base64
import importlib.metadata
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "transcreen"
DEFAULT_OUT = ROOT / "dist" / "Transcreen.py"

# Wheels the app needs at runtime.
_CORE_DISTS = ["pillow", "deep-translator", "requests", "winsdk", "mss"]
_OPTIONAL_DISTS = ["ctranslate2", "sentencepiece"]  # local offline MT tier
_CORE_IMPORTS = ["PIL", "deep_translator", "requests", "winsdk", "mss"]


def _collect_modules() -> dict[str, str]:
    modules: dict[str, str] = {}
    for path in sorted(SRC.glob("*.py")):
        name = "transcreen" if path.stem == "__init__" else f"transcreen.{path.stem}"
        modules[name] = path.read_text(encoding="utf-8")
    if "transcreen" not in modules or "transcreen.app" not in modules:
        raise SystemExit(f"source tree looks wrong under {SRC}")
    return modules


def _pins(dists: list[str]) -> list[str]:
    return [f"{dist}=={importlib.metadata.version(dist)}" for dist in dists]


def build(out: Path) -> None:
    modules = _collect_modules()
    blob = base64.b64encode(zlib.compress(repr(modules).encode("utf-8"), level=9)).decode("ascii")
    lines = "\n".join(f'    "{blob[i : i + 96]}"' for i in range(0, len(blob), 96))
    chunks = f"(\n{lines}\n)"
    template = (Path(__file__).parent / "onefile_template.py").read_text(encoding="utf-8")
    generated = (
        template.replace('"@@BLOB@@"', chunks)
        .replace('"@@CORE_PINS@@"', repr(_pins(_CORE_DISTS)))
        .replace('"@@OPTIONAL_PINS@@"', repr(_pins(_OPTIONAL_DISTS)))
        .replace('"@@CORE_DISTS@@"', repr(_CORE_DISTS))
        .replace('"@@OPTIONAL_DISTS@@"', repr(_OPTIONAL_DISTS))
        .replace('"@@CORE_IMPORTS@@"', repr(_CORE_IMPORTS))
    )
    for marker in ("@@BLOB@@", "@@CORE_PINS@@", "@@CORE_IMPORTS@@"):
        if marker in generated:
            raise SystemExit(f"template marker {marker} was not replaced")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(generated, encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size / 1024:.0f} KiB, {len(modules)} modules)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    build(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
