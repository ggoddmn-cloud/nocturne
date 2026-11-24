#!/usr/bin/env python3
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
import venv
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
TOOLS = ROOT / "tools"
MARKER = VENV / ".nocturne_deps_ok"
APP = ROOT / "bot.py"


def venv_python():
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def run(cmd, **kwargs):
    print(">", " ".join(map(str, cmd)))
    return subprocess.check_call([str(x) for x in cmd], **kwargs)


def create_venv():
    py = venv_python()
    if py.exists():
        return py
    print("[1/3] Создаю локальное окружение Python…")
    venv.EnvBuilder(with_pip=True, clear=False).create(VENV)
    return py


def deps_installed(py):
    if not MARKER.exists():
        return False
    try:
        subprocess.check_call(
            [str(py), "-c", "import yt_dlp, imageio_ffmpeg, telegram, dotenv"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def install_deps(py):
    if deps_installed(py):
        print("[2/3] yt-dlp и ffmpeg уже готовы.")
        return
    print("[2/3] Устанавливаю yt-dlp и локальный ffmpeg (только при первом запуске)…")
    run([py, "-m", "pip", "install", "--disable-pip-version-check", "-U", "-r", str(ROOT / "requirements.txt")])
    MARKER.write_text(str(int(time.time())), encoding="utf-8")


def deno_asset_name():
    sysname = platform.system().lower()
    machine = platform.machine().lower()
    arm64 = machine in {"arm64", "aarch64"}
    if sysname == "windows":
        target = "aarch64-pc-windows-msvc" if arm64 else "x86_64-pc-windows-msvc"
        return f"deno-{target}.zip", "deno.exe"
    if sysname == "darwin":
        target = "aarch64-apple-darwin" if arm64 else "x86_64-apple-darwin"
        return f"deno-{target}.zip", "deno"
    if sysname == "linux":
        target = "aarch64-unknown-linux-gnu" if arm64 else "x86_64-unknown-linux-gnu"
        return f"deno-{target}.zip", "deno"
    return None, None


def ensure_deno():
    TOOLS.mkdir(exist_ok=True)
    asset, binary = deno_asset_name()
    if not asset:
        print("[3/3] Неизвестная ОС: Deno автоматически не устанавливаю.")
        return
    dst = TOOLS / binary
    if dst.exists():
        print("[3/3] Deno уже готов.")
        return
    existing = shutil.which("deno")
    if existing:
        print(f"[3/3] Использую системный Deno: {existing}")
        return
    url = f"https://github.com/denoland/deno/releases/latest/download/{asset}"
    archive = TOOLS / asset
    print("[3/3] Загружаю Deno для полной поддержки YouTube…")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "NocturneVideo/2.0"})
        with urllib.request.urlopen(req, timeout=60) as r, open(archive, "wb") as f:
            shutil.copyfileobj(r, f)
        with zipfile.ZipFile(archive) as zf:
            member = next((n for n in zf.namelist() if Path(n).name == binary), None)
            if not member:
                raise RuntimeError("В архиве Deno не найден исполняемый файл")
            with zf.open(member) as src, open(dst, "wb") as out:
                shutil.copyfileobj(src, out)
        if os.name != "nt":
            dst.chmod(dst.stat().st_mode | 0o111)
        print("Deno установлен локально:", dst)
    except Exception as exc:
        print("ВНИМАНИЕ: Deno не удалось установить:", exc)
        print("Приложение всё равно запустится, но часть форматов YouTube может быть недоступна.")
    finally:
        try:
            archive.unlink()
        except OSError:
            pass


def main():
    if sys.version_info < (3, 11):
        print("Нужен Python 3.11 или новее. Сейчас:", sys.version.split()[0])
        print("Установи свежий Python и снова запусти этот файл.")
        return 2
    if not APP.exists():
        print("bot.py не найден рядом с bootstrap.py")
        return 2
    py = create_venv()
    install_deps(py)
    ensure_deno()
    print("\nЗапускаю Nocturne Video…\n")
    os.chdir(ROOT)
    if os.name == "nt":
        return subprocess.call([str(py), str(APP)])
    os.execv(str(py), [str(py), str(APP)])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print("\nНе удалось подготовить зависимости. Код:", exc.returncode)
        print("Проверь интернет и повтори запуск.")
        raise SystemExit(exc.returncode)
