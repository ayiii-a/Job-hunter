"""路径、环境变量、以及 Windows 上的编码防线。

为什么有 read_text / write_text 这两个包装：
Windows 上 Python 的默认编码是 cp1252，而 JD 文本里满是非 ASCII（实测抓到过
日文岗位标题、smart quotes、em-dash）。裸 open() 迟早会炸出 UnicodeDecodeError，
而且崩的地方离真正的原因很远。所以本项目里读写文件一律走这两个函数。
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录：src/jha/config.py -> src/jha -> src -> 根
ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"

MASTER_PROFILE_PATH = CONFIG_DIR / "master_profile.yaml"
TARGET_PROFILE_PATH = CONFIG_DIR / "target_profile.yaml"
COMPANIES_PATH = CONFIG_DIR / "companies.yaml"

# 只有 *.example.yaml 进 git。真实文件里有你的姓名、电话、住址和完整履历，
# 留在本机（见 .gitignore）。代价是简历没有 git 版本历史——要的话自己另外备份。
CONFIG_TEMPLATES: tuple[tuple[Path, Path], ...] = (
    (CONFIG_DIR / "master_profile.example.yaml", MASTER_PROFILE_PATH),
    (CONFIG_DIR / "target_profile.example.yaml", TARGET_PROFILE_PATH),
    (CONFIG_DIR / "companies.example.yaml", COMPANIES_PATH),
)

load_dotenv(ROOT / ".env")


def bootstrap_configs() -> list[Path]:
    """把缺失的配置文件从模板复制出来，返回新建了哪些。

    新克隆的仓库里只有模板，没有真实文件——不做这一步，第一次跑
    `agent profile check` 就是一句「找不到」。

    已存在的文件绝不覆盖：那是你写了两周的东西。
    """
    created: list[Path] = []
    for template, target in CONFIG_TEMPLATES:
        if target.exists() or not template.exists():
            continue
        write_text(target, read_text(template))
        created.append(target)
    return created


def unchanged_from_template() -> list[Path]:
    """列出内容和模板一模一样、即还没动过的配置文件。

    结构校验查不出这种问题：模板本身是合法的，所以一个字没改也会「通过」。
    而模板里的默认值往往正好是错的（例如 titles_exclude 里的 New Grad
    会把应届岗位全滤掉），于是你会以为筛选在工作，其实它在反着筛。
    """
    stale: list[Path] = []
    for template, target in CONFIG_TEMPLATES:
        if not (template.exists() and target.exists()):
            continue
        if read_text(target) == read_text(template):
            stale.append(target)
    return stale


def force_utf8_stdio() -> None:
    """把 stdout/stderr 钉成 utf-8。

    在 Windows 控制台里 print 一个日文岗位标题会抛 UnicodeEncodeError——
    不是文件的问题，是 stdout 的问题。CLI 入口调一次即可。
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if isinstance(stream, io.TextIOWrapper) and stream.encoding.lower() not in (
            "utf-8",
            "utf8",
        ):
            stream.reconfigure(encoding="utf-8", errors="replace")


def read_text(path: str | Path) -> str:
    """读文本。永远 utf-8。"""
    return Path(path).read_text(encoding="utf-8")


def write_text(path: str | Path, content: str) -> None:
    """写文本。永远 utf-8 + LF，避免 Windows 上换行符污染 diff。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with io.open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)


def env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


def env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def db_path() -> Path:
    """数据库位置。相对路径按项目根解析。"""
    raw = env("JHA_DB_PATH", "data/jha.db") or "data/jha.db"
    p = Path(raw)
    return p if p.is_absolute() else ROOT / p


def ghost_after_days() -> int:
    return env_int("JHA_GHOST_AFTER_DAYS", 30)


def daily_apply_limit() -> int:
    return env_int("JHA_DAILY_APPLY_LIMIT", 8)
