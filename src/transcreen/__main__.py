"""Entry point: python -m transcreen [--selftest] [--smoke DIR] [--setup-local [FILE]]."""

from __future__ import annotations

import argparse
import logging

from .config import Config, config_dir
from .screen import enable_dpi_awareness


def _setup_logging() -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        config_dir().mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(config_dir() / "app.log", encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="transcreen", description="On-screen text translator for Windows"
    )
    parser.add_argument(
        "--selftest", action="store_true", help="check the OCR+translation pipeline, no GUI"
    )
    parser.add_argument(
        "--smoke", metavar="DIR", help="launch the UI briefly and save screenshots to DIR"
    )
    parser.add_argument(
        "--setup-local",
        nargs="?",
        const="",
        default=None,
        metavar="FILE",
        help="install the local en->tr model (download it, or read FILE), then exit",
    )
    parser.add_argument(
        "--small-model",
        action="store_true",
        help="with --setup-local: install the lighter, less accurate Argos model",
    )
    args = parser.parse_args(argv)

    _setup_logging()
    enable_dpi_awareness()

    if args.setup_local is not None:
        from . import local_mt

        try:
            local_mt.setup(
                local_mt.default_model_dir(),
                source_file=args.setup_local or None,
                small=args.small_model,
            )
        except local_mt.LocalMtError as exc:
            print(f"HATA: Lokal model kurulamadı: {exc}")
            return 1
        return 0

    if args.selftest:
        from .selftest import run_selftest

        return run_selftest()

    from . import winfx

    if args.smoke is None and not winfx.claim_single_instance():
        logging.getLogger(__name__).warning(
            "Transcreen is already running; this copy exits so the two "
            "overlays cannot fight over the same screen"
        )
        print("Transcreen zaten çalışıyor — bu kopya kapatıldı.")
        return 0

    from .app import App

    App(Config.load(), smoke_dir=args.smoke).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
