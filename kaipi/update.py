"""GitHub release checks and an out-of-process updater for uv tool installations."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any

import httpx
from packaging.version import Version

REPO = "https://github.com/jingchaoqi/kaipi"
API = "https://api.github.com/repos/jingchaoqi/kaipi/releases/latest"


def current() -> str:
    return version("kaipi")


def cache_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "kaipi"


def _read_cache() -> dict[str, Any]:
    try:
        value = json.loads((cache_dir() / "update.json").read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(value: dict[str, Any]) -> None:
    try:
        root = cache_dir()
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=root, delete=False) as stream:
            json.dump(value, stream)
        os.replace(stream.name, root / "update.json")
    except OSError:
        pass  # a read-only cache must not prevent using kaipi


@dataclass(frozen=True)
class Release:
    tag: str
    assets: dict[str, str]

    @property
    def url(self) -> str:
        return f"{REPO}/releases/tag/{self.tag}"

    @property
    def newer(self) -> bool:
        return Version(self.tag) > Version(current())

    def describe(self) -> str:
        return f"当前版本 {current()} → 最新版本 {self.tag}\n更新内容：{self.url}"


def check(*, force: bool = False) -> Release | None:
    cache = _read_cache()
    if force or time.time() - float(cache.get("checked", 0)) >= 86400:
        # Cache failed attempts too: offline startups must not retry on every launch.
        cache["checked"] = time.time()
        cache["release"] = None
        _save_cache(cache)
        with httpx.Client(timeout=5, follow_redirects=True) as client:
            response = client.get(API, headers={"Accept": "application/vnd.github+json"})
            response.raise_for_status()
            data = response.json()
        if data.get("draft") or data.get("prerelease"):
            return None
        tag = data["tag_name"]
        parsed = Version(tag)
        if parsed.is_prerelease or parsed.is_devrelease:
            return None
        cache["release"] = {
            "tag": tag,
            "assets": {a["name"]: a["browser_download_url"] for a in data["assets"]},
        }
        _save_cache(cache)
    data = cache.get("release")
    return Release(**data) if data else None


def background(emit: Callable[[str], None]) -> None:
    if os.environ.get("KAIPI_NO_UPDATE_CHECK"):
        return

    def work() -> None:
        try:
            release = check()
            cache = _read_cache()
            if release and release.newer and cache.get("notified") != release.tag:
                emit(f"发现新版！{release.describe()}\n输入 /update 更新，并接回当前会话。")
                cache["notified"] = release.tag
                _save_cache(cache)
        except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError):
            pass

    threading.Thread(target=work, daemon=True).start()


def installation() -> tuple[str, Path]:
    """Target the running tool's receipt, never an unrelated kaipi found on PATH."""
    uv = shutil.which("uv")
    prefix = Path(sys.prefix)
    try:
        receipt = tomllib.loads((prefix / "uv-receipt.toml").read_text())["tool"]
        entry = next(e for e in receipt["entrypoints"] if e["name"] == "kaipi")
        executable = Path(entry["install-path"])
        owned = Path(__file__).resolve().is_relative_to(prefix.resolve())
        if uv and prefix.name == "kaipi" and owned and executable.is_file():
            return uv, executable
    except (OSError, ValueError, KeyError, StopIteration, TypeError):
        pass
    raise ValueError(
        "当前不是可识别的 uv tool 安装，暂不自动替换。源码安装请在源码目录手动更新并运行 uv sync；"
        f"其他安装方式请从 {REPO}/releases/latest 下载新版安装包。"
    )


@dataclass(frozen=True)
class Plan:
    uv: str
    executable: Path
    wheel: Path
    tag: str


class Ready(Exception):
    def __init__(self, plan: Plan) -> None:
        self.plan = plan


def prepare(emit: Callable[[str], None] = print) -> Plan | None:
    emit(f"当前版本 {current()}，正在检查新版…")
    try:
        release = check(force=True)
    except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError) as e:
        raise ValueError(f"暂时无法检查更新，请稍后再试。也可访问 {REPO}/releases/latest") from e
    if release is None:
        emit(f"暂未找到正式版本。请查看 {REPO}/releases")
        return None
    emit(release.describe())
    if not release.newer:
        emit(
            "已是最新正式版，无需更新。"
            if Version(release.tag) == Version(current())
            else "当前版本不低于最新正式版，无需更新。"
        )
        return None
    uv, executable = installation()
    name = f"kaipi-{Version(release.tag)}-py3-none-any.whl"
    directory: Path | None = None
    try:
        # Only release assets belonging to this repository may become executable code.
        urls = [release.assets[key] for key in (name, "SHA256SUMS")]
        if any(not url.startswith(f"{REPO}/releases/download/{release.tag}/") for url in urls):
            raise ValueError("安装包地址不属于 kaipi Release")
        emit("正在下载并校验安装包；完成前不会退出当前会话…")
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            checksum = client.get(urls[1])
            checksum.raise_for_status()
            hashes = [
                parts[0]
                for line in checksum.text.splitlines()
                if len(parts := line.split()) == 2 and parts[1].lstrip("*") == name
            ]
            if len(hashes) != 1:
                raise ValueError("缺少唯一的安装包校验值")
            response = client.get(urls[0])
            response.raise_for_status()
            if hashlib.sha256(response.content).hexdigest() != hashes[0]:
                raise ValueError("安装包 SHA-256 校验失败")
        cache_dir().mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="update-", dir=cache_dir()))
        wheel = directory / name
        wheel.write_bytes(response.content)
        return Plan(uv, executable, wheel, release.tag)
    except (httpx.HTTPError, OSError, ValueError, KeyError) as e:
        if directory:
            shutil.rmtree(directory, ignore_errors=True)
        raise ValueError(
            f"下载或校验未完成，尚未替换程序，可稍后重试。版本说明：{release.url}"
        ) from e


# This worker imports only stdlib, before uv replaces the running installation.
# Keep the verified wheel: uv records its path as the tool's installation source.
WORKER = """
import os, shlex, subprocess, sys
uv, wheel, executable, tag, resume, tools = sys.argv[1:]
os.environ['UV_TOOL_DIR'] = tools
os.environ['UV_TOOL_BIN_DIR'] = os.path.dirname(executable)
command = [uv, 'tool', 'install', '--quiet', '--force', wheel]
print('正在安装 ' + tag + '，请稍候…', flush=True)
try:
    result = subprocess.run(command, cwd=os.path.dirname(wheel)).returncode
except OSError:
    result = 1
if result:
    print('更新未完成，已保存的会话和配置不受影响。可重试：', shlex.join(command), flush=True)
    if resume:
        print('安装成功后继续会话：', shlex.join([executable, '-r', resume]), flush=True)
    sys.exit(result)
print('已更新至 ' + tag + ('，正在接回原会话。' if resume else '。'), flush=True)
if resume:
    os.execv(executable, [executable, '-r', resume])
"""


def install(plan: Plan, *, resume: str = "") -> None:
    sys.stdout.flush()
    os.execv(
        sys.executable,
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            WORKER,
            plan.uv,
            str(plan.wheel),
            str(plan.executable),
            plan.tag,
            resume,
            str(Path(sys.prefix).parent),
        ],
    )
