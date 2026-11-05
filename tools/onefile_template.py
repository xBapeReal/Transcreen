r"""Transcreen — tek dosyalık taşınabilir sürüm.

Bu dosya otomatik üretildi (tools/build_onefile.py) — elle düzenlemeyin.

Kullanım: bu dosyayı istediğiniz klasöre veya bilgisayara kopyalayın ve
çalıştırın (çift tık veya `python Transcreen.py`). Hedef makinede tek
gereksinim: Windows 10/11 + python.org'dan Python 3.10–3.13 (varsayılan
kurulum; tcl/tk dahil). İlk çalıştırmada gerekli paketleri tek seferlik
%LOCALAPPDATA%\Transcreen içine kurar ve kendini oradan yeniden başlatır;
sonraki açılışlar doğrudandır. Ayarlar, önbellek ve modeller her zaman
%APPDATA%\Transcreen klasöründedir — dosyayı taşımak bunları etkilemez.

Lokal (çevrimdışı) çeviri modeli için: python Transcreen.py --setup-local
Kısayollar: Ctrl+Alt+T duraklat/devam • Ctrl+Alt+S ekran görüntüsü • Ctrl+Alt+Q çıkış
"""

from __future__ import annotations

import base64
import importlib
import importlib.abc
import importlib.util
import os
import subprocess
import sys
import zlib
from pathlib import Path
from typing import NoReturn

_BLOB_B64 = "@@BLOB@@"
_CORE_PINS = "@@CORE_PINS@@"
_OPTIONAL_PINS = "@@OPTIONAL_PINS@@"
_CORE_DISTS = "@@CORE_DISTS@@"
_OPTIONAL_DISTS = "@@OPTIONAL_DISTS@@"
_CORE_IMPORTS = "@@CORE_IMPORTS@@"

_PKG = "transcreen"
_RELAUNCH_FLAG = "TRANSCREEN_ONEFILE_RELAUNCHED"


def _decode_modules() -> dict[str, str]:
    import ast

    raw = zlib.decompress(base64.b64decode(_BLOB_B64)).decode("utf-8")
    modules = ast.literal_eval(raw)
    assert isinstance(modules, dict)
    return modules


class _EmbeddedLoader(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Serves the embedded transcreen package from memory."""

    def __init__(self, modules: dict[str, str]) -> None:
        self._modules = modules

    def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001, ANN201
        if fullname not in self._modules:
            return None
        return importlib.util.spec_from_loader(fullname, self, is_package=fullname == _PKG)

    def create_module(self, spec):  # noqa: ANN001, ANN201
        return None

    def exec_module(self, module) -> None:  # noqa: ANN001
        name = module.__name__
        module.__file__ = f"<Transcreen:{name}>"
        exec(compile(self._modules[name], module.__file__, "exec"), module.__dict__)


def _fatal(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    if not sys.argv[1:]:  # GUI launch: there may be no console to read
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "Transcreen", 0x10)
        except Exception:  # noqa: BLE001 - the box is best-effort only
            pass
    sys.exit(1)


def _missing_core() -> list[str]:
    return [name for name in _CORE_IMPORTS if importlib.util.find_spec(name) is None]


def _runtime_venv() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    tag = f"py{sys.version_info.major}{sys.version_info.minor}"
    return Path(base) / "Transcreen" / f"runtime-{tag}" / "venv"


def _pip_install(python: Path, packages: list[str]) -> int:
    command = [
        str(python),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-warn-script-location",
        *packages,
    ]
    env = {**os.environ, "PYTHONUTF8": "1"}
    try:
        return subprocess.run(command, env=env).returncode
    except OSError:
        return 1


def _repair_in_place() -> None:
    """We ARE the runtime venv but packages are missing (interrupted install)."""
    here = Path(sys.executable)
    if _pip_install(here, _CORE_PINS) != 0:
        _pip_install(here, _CORE_DISTS)
    if _pip_install(here, _OPTIONAL_PINS) != 0:
        _pip_install(here, _OPTIONAL_DISTS)
    importlib.invalidate_caches()
    still = _missing_core()
    if still:
        _fatal(
            "Transcreen bağımlılıkları kurulamadı: "
            + ", ".join(still)
            + "\nİnternet bağlantısını kontrol edip yeniden deneyin."
        )


def _bootstrap() -> NoReturn:
    """First run on this machine: build a private venv, install, relaunch."""
    print("Transcreen ilk kurulum: paketler indiriliyor (tek seferlik, 1-3 dk)...")
    venv_dir = _runtime_venv()
    python = venv_dir / "Scripts" / "python.exe"
    if not python.exists():
        venv_dir.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run([sys.executable, "-m", "venv", str(venv_dir)])
        if result.returncode != 0 or not python.exists():
            _fatal(
                "Sanal ortam oluşturulamadı (python -m venv başarısız).\n"
                "python.org'dan standart Python 3.11/3.12 kurulumu önerilir."
            )
    if _pip_install(python, _CORE_PINS) != 0 and _pip_install(python, _CORE_DISTS) != 0:
        _fatal(
            "Zorunlu paketler kurulamadı (pip hata verdi).\n"
            "İnternet bağlantısını kontrol edip bu dosyayı yeniden çalıştırın."
        )
    if _pip_install(python, _OPTIONAL_PINS) != 0 and _pip_install(python, _OPTIONAL_DISTS) != 0:
        print("Not: lokal çeviri paketleri kurulamadı; uygulama bulut çevirisiyle çalışacak.")
    env = {**os.environ, _RELAUNCH_FLAG: "1", "PYTHONUTF8": "1"}
    script = str(Path(__file__).resolve())
    arguments = sys.argv[1:]
    if arguments:
        # CLI use (--selftest, --setup-local, ...): stay attached to the console.
        raise SystemExit(subprocess.run([str(python), script, *arguments], env=env).returncode)
    pythonw = venv_dir / "Scripts" / "pythonw.exe"
    launcher = pythonw if pythonw.exists() else python
    subprocess.Popen([str(launcher), script], env=env)
    print("Kurulum tamam — Transcreen arka planda başlatıldı.")
    raise SystemExit(0)


def main() -> int:
    if sys.platform != "win32":
        _fatal("Transcreen yalnız Windows'ta çalışır (Windows OCR motorunu kullanır).")
    if not ((3, 10) <= sys.version_info[:2] <= (3, 13)):
        _fatal(
            f"Python 3.10–3.13 gerekli; bu makinede {sys.version.split()[0]} var.\n"
            "python.org'dan Python 3.11 veya 3.12 kurun."
        )
    if _missing_core():
        if os.environ.get(_RELAUNCH_FLAG):
            _repair_in_place()
        else:
            _bootstrap()

    sys.meta_path.insert(0, _EmbeddedLoader(_decode_modules()))

    if "--verify-embed" in sys.argv[1:]:
        modules = _decode_modules()
        for name in sorted(modules):
            importlib.import_module(name)
        print(f"EMBED OK {len(modules)}")
        return 0

    from transcreen.__main__ import main as app_main

    return app_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
