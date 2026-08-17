#!/usr/bin/env python3
"""Portable capability inventory and Chinese catalog renderer.

The script never installs a skill or plugin. Installation is deliberately left
to the hosting agent's trusted installer; this module discovers, verifies,
diffs, and renders metadata using only the Python standard library.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

try:
    import tomllib
except ModuleNotFoundError as exc:  # pragma: no cover - explicit runtime guard
    raise SystemExit("Python 3.11 or newer is required") from exc


SCHEMA_VERSION = 1
MAX_METADATA_BYTES = 1_000_000
MAX_REMOTE_BYTES = 1_000_000
MAX_SCAN_DEPTH = 8
LOCK_STALE_SECONDS = 30 * 60
VERIFICATION_VALUES = {"已验证", "部分推断", "待核验"}
AGENT_ADAPTERS: dict[str, dict[str, Any]] = {
    "codex": {
        "skill_installer": "skill-installer",
        "plugin_installer": "plugin-management",
        "supports_plugins": True,
        "verification": "rescan-local-metadata",
    },
    "dsh": {
        "skill_installer": "none",
        "plugin_installer": "dsh plugin --profile <profile> add <package-or-github-url>",
        "supports_plugins": True,
        "verification": "rescan-local-metadata",
        "skill_install_note": (
            "DeepSeek Harness has no separate skill installer command. Install a "
            "skill-only repository by copying its SKILL.md directory into "
            "$DSH_HOME/skills after user approval, or install it as a DSH bundle "
            "plugin with `dsh plugin --profile <profile> add <target>`."
        ),
    },
}

CATEGORY_ORDER = [
    "开发与代码",
    "调试与测试",
    "设计与前端",
    "图片与多媒体",
    "文档、表格与演示",
    "研究与知识管理",
    "自动化与工作流",
    "外部服务与集成",
    "DeepSeek Harness 系统与扩展",
    "Codex 系统与扩展",
    "其他",
]
CATEGORY_RULES = [
    ("调试与测试", ("debug", "diagnos", "test", "tdd", "review", "merge conflict", "调试", "测试", "审查")),
    ("设计与前端", ("frontend", "design", "ux", "ui", "website", "site", "brand", "stitch", "前端", "设计", "网站")),
    ("图片与多媒体", ("image", "video", "audio", "remotion", "heygen", "图片", "图像", "视频", "音频")),
    ("文档、表格与演示", ("document", "docx", "pdf", "spreadsheet", "excel", "presentation", "slide", "文档", "表格", "演示")),
    ("研究与知识管理", ("research", "notion", "zotero", "knowledge", "研究", "知识")),
    ("自动化与工作流", ("automat", "workflow", "calendar", "asana", "linear", "任务", "工作流", "自动化")),
    ("外部服务与集成", ("plugin", "marketplace", "github", "slack", "gmail", "drive", "cloudflare", "vercel", "stripe", "supabase", "集成", "插件", "市场")),
    ("DeepSeek Harness 系统与扩展", ("deepseek", "dsh", "harness", "dsh-home")),
    ("Codex 系统与扩展", ("codex", "skill", "agent", "installer", "catalog", "能力目录", "技能")),
    ("开发与代码", ("code", "coding", "program", "api", "database", "prototype", "代码", "开发", "编程")),
]


class CatalogError(RuntimeError):
    """A user-actionable catalog failure."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-.")
    return value or "unknown"


def canonical_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve(strict=False)


def absolute_path(value: str | Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(value))))


def is_directory_link(path: Path) -> bool:
    """Recognize POSIX symlinks and Windows directory junctions."""
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction):
        with contextlib.suppress(OSError):
            return bool(is_junction())
    if os.name == "nt":
        with contextlib.suppress(OSError):
            return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    return False


def path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def read_limited(path: Path, limit: int = MAX_METADATA_BYTES) -> str:
    try:
        with path.open("rb") as handle:
            raw = handle.read(limit + 1)
    except OSError as exc:
        raise CatalogError(f"Cannot read {path}: {exc}") from exc
    if len(raw) > limit:
        raise CatalogError(f"Metadata file exceeds {limit} bytes: {path}")
    return raw.decode("utf-8-sig", errors="replace")


def json_load(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(read_limited(path, 10_000_000))
    except (json.JSONDecodeError, CatalogError) as exc:
        raise CatalogError(f"Invalid JSON in {path}: {exc}") from exc


def atomic_write(path: Path, text: str, *, backup: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise


class FileLock:
    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, json.dumps({"pid": os.getpid(), "created_at": utc_now()}).encode())
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except OSError:
                    age = 0
                if age > LOCK_STALE_SECONDS and attempt == 0:
                    with contextlib.suppress(OSError):
                        self.path.unlink()
                    continue
                raise CatalogError(f"Another catalog update holds {self.path}")
        raise CatalogError(f"Could not acquire {self.path}")

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.fd is not None:
            os.close(self.fd)
        with contextlib.suppress(OSError):
            self.path.unlink()


def default_config_path() -> Path:
    override = os.environ.get("CAPABILITY_CATALOG_CONFIG")
    if override:
        return canonical_path(override)
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "catalog-capabilities-zh" / "config.json"


def empty_config() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "active_profile": "default", "profiles": {}}


def load_config(path: Path) -> dict[str, Any]:
    config = json_load(path, empty_config())
    if not isinstance(config, dict) or config.get("schema_version") != SCHEMA_VERSION:
        raise CatalogError(f"Unsupported config schema in {path}")
    config.setdefault("profiles", {})
    config.setdefault("active_profile", "default")
    return config


def state_path(config_path: Path, profile: str) -> Path:
    return config_path.with_name(f"state.{slug(profile)}.json")


def normalize_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return url.strip()
    host = parsed.netloc.lower()
    path = re.sub(r"/+", "/", parsed.path).rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return urllib.parse.urlunparse(("https", host, path, "", "", ""))


def parse_frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}
    result: dict[str, str] = {}
    i = 1
    while i < end:
        match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", lines[i])
        if not match:
            i += 1
            continue
        key, raw = match.groups()
        raw = raw.strip()
        if raw in {"|", ">", "|-", ">-"}:
            chunks: list[str] = []
            i += 1
            while i < end and (not lines[i].strip() or lines[i].startswith((" ", "\t"))):
                chunks.append(lines[i].strip())
                i += 1
            result[key] = " ".join(part for part in chunks if part)
            continue
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
            raw = raw[1:-1]
        result[key] = raw
        i += 1
    return result


def parse_openai_yaml(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for key in ("display_name", "short_description", "default_prompt"):
        match = re.search(rf"^\s*{key}:\s*['\"]?(.*?)['\"]?\s*$", text, re.MULTILINE)
        if match:
            result[key] = match.group(1)
    return result


def hash_metadata(files: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(set(files), key=lambda item: str(item).lower()):
        digest.update(path.name.encode("utf-8"))
        try:
            digest.update(read_limited(path).encode("utf-8"))
        except CatalogError as exc:
            digest.update(str(exc).encode("utf-8"))
    return digest.hexdigest()


def walk_named(root: Path, filename: str, max_depth: int = MAX_SCAN_DEPTH) -> Iterator[Path]:
    if max_depth < 0:
        raise CatalogError("scan depth must be zero or greater")
    if not root.is_dir():
        return
    root = root.resolve(strict=False)
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        try:
            depth = len(current_path.relative_to(root).parts)
        except ValueError:
            dirs[:] = []
            continue
        dirs[:] = [
            name
            for name in dirs
            if depth < max_depth
            and not is_directory_link(current_path / name)
            and path_is_within(current_path / name, root)
        ]
        if filename in files:
            matched = current_path / filename
            if not matched.is_symlink() and path_is_within(matched.resolve(strict=False), root):
                yield matched


def normalize_root_entries(values: Iterable[Any], prefix: str) -> list[dict[str, Any]]:
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, value in enumerate(values, start=1):
        metadata: dict[str, Any] = {}
        if isinstance(value, dict):
            raw_path = value.get("path")
            alias = value.get("alias") or f"{prefix}-{index}"
            metadata = {
                key: value[key]
                for key in ("group", "source_link", "confirmed", "source")
                if key in value
            }
        else:
            raw_path = value
            alias = f"{prefix}-{index}"
        if not raw_path:
            continue
        source_path = absolute_path(raw_path)
        path = canonical_path(source_path)
        key = os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        entry: dict[str, Any] = {"path": str(path), "alias": slug(str(alias))}
        if is_directory_link(source_path):
            entry.update({
                "source_link": str(source_path),
                "group": metadata.get("group") or source_path.name,
                "source": metadata.get("source") or "linked-root",
                "confirmed": bool(metadata.get("confirmed", False)),
            })
        else:
            entry.update(metadata)
        entries.append(entry)
    return entries


def discover_linked_roots(group_root: Path, max_depth: int = 3, scan_depth: int = MAX_SCAN_DEPTH) -> dict[str, Any]:
    """List directory links beneath a grouping directory without recursively following them."""
    group_root = absolute_path(group_root)
    if not group_root.is_dir():
        raise CatalogError(f"Linked-root group is not a readable directory: {group_root}")
    if max_depth < 1 or scan_depth < 0:
        raise CatalogError("link discovery depth must be positive and scan depth non-negative")
    candidates: list[dict[str, Any]] = []
    diagnostics: list[str] = []
    visited: set[str] = set()

    def visit(directory: Path, depth: int) -> None:
        canonical = os.path.normcase(str(directory.resolve(strict=False)))
        if canonical in visited:
            diagnostics.append(f"Directory cycle or duplicate skipped: {directory}")
            return
        visited.add(canonical)
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name.lower())
        except OSError as exc:
            diagnostics.append(f"Cannot inspect grouping directory {directory}: {exc}")
            return
        for child in children:
            if not child.is_dir():
                continue
            if is_directory_link(child):
                target = child.resolve(strict=False)
                exists = target.is_dir()
                candidates.append({
                    "group": child.name,
                    "source_link": str(child),
                    "resolved_path": str(target),
                    "exists": exists,
                    "readable": exists and os.access(target, os.R_OK),
                    "writable": exists and os.access(target, os.W_OK),
                    "item_count": root_item_count(target, "SKILL.md", max_depth=scan_depth) if exists else 0,
                    "confirmed": False,
                })
            elif depth < max_depth:
                visit(child, depth + 1)

    visit(group_root, 0)
    unique: dict[str, dict[str, Any]] = {}
    for item in candidates:
        key = os.path.normcase(item["resolved_path"])
        if key in unique:
            diagnostics.append(
                f"Duplicate linked target: {item['source_link']} -> {item['resolved_path']}"
            )
            continue
        unique[key] = item
    return {
        "group_root": str(group_root),
        "candidates": sorted(unique.values(), key=lambda item: (item["group"].lower(), item["resolved_path"].lower())),
        "diagnostics": sorted(set(diagnostics)),
        "security": "Candidates are read-only discoveries. Pass selected links with configure --linked-root to confirm them.",
    }


def root_item_count(path: Path, filename: str, max_depth: int = 4) -> int:
    count = 0
    for _ in walk_named(path, filename, max_depth=max_depth):
        count += 1
        if count >= 10_000:
            break
    return count


def candidate(path: Path, kind: str, reason: str, filename: str) -> dict[str, Any]:
    exists = path.exists()
    return {
        "kind": kind,
        "path": str(path),
        "reason": reason,
        "exists": exists,
        "readable": exists and os.access(path, os.R_OK),
        "writable": exists and os.access(path, os.W_OK),
        "item_count": root_item_count(path, filename) if exists else 0,
    }


def dsh_home_dir(explicit: str | None = None) -> Path:
    return canonical_path(explicit or os.environ.get("DSH_HOME") or Path.home() / ".dsh")


def dsh_profile_dir(home: Path | str, profile: str) -> Path:
    return canonical_path(home) / "profiles" / (profile or "web")


def dsh_plugin_roots(home: Path | str, preferred_profile: str = "web") -> list[tuple[Path, str]]:
    """Return one plugin root per initialized DSH profile, preferring node_modules."""
    home_path = canonical_path(home)
    profiles = home_path / "profiles"
    roots: list[tuple[Path, str]] = []
    if profiles.is_dir():
        for child in sorted(profiles.iterdir(), key=lambda item: item.name.lower()):
            if not child.is_dir():
                continue
            node_modules = child / "node_modules"
            roots.append((node_modules if node_modules.is_dir() else child, child.name))
    if not roots:
        roots.append((dsh_profile_dir(home_path, preferred_profile), preferred_profile))
    return roots


def iter_top_level_dsh_packages(root: Path) -> Iterator[Path]:
    """Yield top-level package directories without descending into dependency trees."""
    if not root.is_dir():
        return
    if (root / "package.json").is_file():
        if root.parent.name == "profiles":
            node_modules = root / "node_modules"
            if node_modules.is_dir():
                yield from iter_top_level_dsh_packages(node_modules)
            return
        yield root
        return
    if root.name == "node_modules":
        for child in sorted(root.iterdir(), key=lambda item: item.name.lower()):
            if not child.is_dir():
                continue
            if child.name.startswith("@"):
                for scoped in sorted(child.iterdir(), key=lambda item: item.name.lower()):
                    if scoped.is_dir() and (scoped / "package.json").is_file():
                        yield scoped
            elif (child / "package.json").is_file():
                yield child
        return
    for child in sorted(root.iterdir(), key=lambda item: item.name.lower()):
        if not child.is_dir():
            continue
        if child.name == "node_modules":
            yield from iter_top_level_dsh_packages(child)
        elif (child / "node_modules").is_dir():
            yield from iter_top_level_dsh_packages(child / "node_modules")


def dsh_package_count(root: Path, limit: int = 10_000) -> int:
    count = 0
    for _ in iter_top_level_dsh_packages(root):
        count += 1
        if count >= limit:
            break
    return count


def discover_paths(
    config_path: Path,
    codex_home: str | None = None,
    dsh_home: str | None = None,
) -> dict[str, Any]:
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    diagnostics: list[str] = []

    def add(path: Path, kind: str, reason: str, filename: str, item_count: int | None = None) -> None:
        key = (kind, os.path.normcase(str(path.resolve(strict=False))))
        if key not in seen:
            seen.add(key)
            entry = candidate(path, kind, reason, filename)
            if item_count is not None:
                entry["item_count"] = item_count
            found.append(entry)

    try:
        config = load_config(config_path)
    except CatalogError as exc:
        config = empty_config()
        diagnostics.append(str(exc))
    if not codex_home and not dsh_home:
        for profile_name, profile in config.get("profiles", {}).items():
            for entry in normalize_root_entries(profile.get("skill_roots", []), "skills"):
                add(Path(entry["path"]), "skills", f"saved profile {profile_name}", "SKILL.md")
            for entry in normalize_root_entries(profile.get("plugin_roots", []), "plugins"):
                add(Path(entry["path"]), "plugins", f"saved profile {profile_name}", "package.json")

    codex_homes: list[tuple[Path, str]] = []
    if codex_home:
        codex_homes.append((canonical_path(codex_home), "explicit --codex-home"))
    elif not dsh_home:
        if os.environ.get("CODEX_HOME"):
            codex_homes.append((canonical_path(os.environ["CODEX_HOME"]), "CODEX_HOME"))
        else:
            codex_homes.append((canonical_path(Path.home() / ".codex"), "Codex standard directory"))
    for home, reason in codex_homes:
        add(home / "skills", "skills", reason, "SKILL.md")
        add(home / "plugins", "plugins", reason, "plugin.json")
        add(home / "config.toml", "config", reason, "config.toml")

    dsh_homes: list[tuple[Path, str]] = []
    if dsh_home:
        dsh_homes.append((canonical_path(dsh_home), "explicit --dsh-home"))
    elif not codex_home:
        if os.environ.get("DSH_HOME"):
            dsh_homes.append((canonical_path(os.environ["DSH_HOME"]), "DSH_HOME"))
        else:
            dsh_homes.append((canonical_path(Path.home() / ".dsh"), "DeepSeek Harness standard directory"))
    for home, reason in dsh_homes:
        add(home / "skills", "skills", reason, "SKILL.md")
        for plugin_root, profile_name in dsh_plugin_roots(home):
            add(
                plugin_root,
                "plugins",
                f"{reason} - profile {profile_name}",
                "package.json",
                item_count=dsh_package_count(plugin_root),
            )
        web_profile = dsh_profile_dir(home, "web")
        add(web_profile / "package.json", "config", f"{reason} - web profile manifest", "package.json")
        add(home / "cordis.patch.yml", "config", reason, "cordis.patch.yml")

    skill_dir = Path(__file__).resolve().parents[1]
    if skill_dir.parent.name == "skills":
        add(skill_dir.parent, "skills", "current skill location", "SKILL.md")

    found.sort(key=lambda item: (item["kind"], not item["exists"], item["path"].lower()))
    return {
        "schema_version": SCHEMA_VERSION,
        "config_path": str(config_path),
        "candidates": found,
        "diagnostics": diagnostics,
    }


def read_codex_config(path: Path | None) -> tuple[dict[str, bool], dict[str, dict[str, Any]], list[str]]:
    if path is None or not path.exists():
        return {}, {}, []
    diagnostics: list[str] = []
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return {}, {}, [f"Invalid Codex config {path}: {exc}"]
    plugins: dict[str, bool] = {}
    for key, value in data.get("plugins", {}).items():
        plugins[str(key).lower()] = bool(value.get("enabled", False)) if isinstance(value, dict) else False
    markets = data.get("marketplaces", {})
    return plugins, markets if isinstance(markets, dict) else {}, diagnostics


def read_dsh_profile_manifest(path: Path | None) -> tuple[set[str], set[str], list[str]]:
    """Read the active bundle list and dependency names from a DSH profile manifest."""
    diagnostics: list[str] = []
    if path is None or not path.is_file():
        return set(), set(), diagnostics
    try:
        data = json_load(path, {})
    except CatalogError as exc:
        return set(), set(), [str(exc)]
    if not isinstance(data, dict):
        return set(), set(), [f"DSH profile manifest is not an object: {path}"]
    dsh_section = data.get("dsh")
    profile_section = dsh_section.get("profile") if isinstance(dsh_section, dict) else {}
    bundles = set()
    if isinstance(profile_section, dict):
        raw_bundles = profile_section.get("bundles") or []
        if isinstance(raw_bundles, list):
            bundles = {str(item) for item in raw_bundles if item}
        else:
            diagnostics.append(f"DSH profile bundles must be a list: {path}")
    dependencies = set()
    raw_dependencies = data.get("dependencies")
    if isinstance(raw_dependencies, dict):
        dependencies = {str(item) for item in raw_dependencies if item}
    return bundles, dependencies, diagnostics


def read_dsh_patch_names(path: Path | None) -> set[str]:
    """Collect plugin names mounted by a profile cordis.patch.yml."""
    if path is None or not path.is_file():
        return set()
    with contextlib.suppress(CatalogError):
        text = read_limited(path)
        return set(
            match.group(1)
            for match in re.finditer(r"^\s*name:\s*['\"]?([A-Za-z0-9@._/-]+)", text, re.MULTILINE)
        )
    return set()


def collect_dsh_profile_states(home: Path) -> dict[str, tuple[set[str], set[str]]]:
    """Index every initialized DSH profile as profile-name -> (active, dependency) names."""
    states: dict[str, tuple[set[str], set[str]]] = {}
    profiles = home / "profiles"
    if not profiles.is_dir():
        return states
    for profile_dir in sorted(profiles.iterdir(), key=lambda item: item.name.lower()):
        if not profile_dir.is_dir():
            continue
        manifest = profile_dir / "package.json"
        patch = profile_dir / "cordis.patch.yml"
        if not manifest.is_file() and not patch.is_file():
            continue
        bundles, dependencies, _ = read_dsh_profile_manifest(manifest)
        patch_names = read_dsh_patch_names(patch)
        states[profile_dir.name] = (set(bundles) | patch_names, dependencies)
    return states


def dsh_origin_from_path(package_dir: Path) -> str:
    parts = package_dir.resolve(strict=False).parts
    if "profiles" in parts:
        index = parts.index("profiles")
        if len(parts) > index + 1:
            return parts[index + 1]
    if "node_modules" in parts:
        return "node_modules"
    return package_dir.parent.name or "dsh"


def repository_url(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("url")
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    if candidate.startswith("github:"):
        candidate = "https://github.com/" + candidate.removeprefix("github:").lstrip("/")
    elif candidate.startswith("git+"):
        candidate = candidate[4:]
    parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return normalize_url(candidate)


def scan_dsh_plugin_package(
    package_dir: Path,
    alias: str,
    active_names: set[str],
    dependency_names: set[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Scan one top-level DSH npm package and return its capability item(s)."""
    diagnostics: list[str] = []
    manifest = package_dir / "package.json"
    try:
        data = json_load(manifest, None)
    except CatalogError as exc:
        return [], [str(exc)]
    if not isinstance(data, dict):
        return [], [f"DSH plugin manifest is not an object: {manifest}"]

    plugin_meta_path = package_dir / "dsh.plugin.json"
    plugin_meta: dict[str, Any] = {}
    if plugin_meta_path.is_file():
        with contextlib.suppress(CatalogError):
            loaded = json_load(plugin_meta_path, None)
            if isinstance(loaded, dict):
                plugin_meta = loaded

    dsh_section = data.get("dsh")
    dshx_section = data.get("dshx")
    plugin_dsh_keys = set(dsh_section) - {"profile"} if isinstance(dsh_section, dict) else set()
    has_dsh = bool(plugin_dsh_keys)
    has_dshx = isinstance(dshx_section, dict) and bool(dshx_section)
    if not has_dsh and not has_dshx and not plugin_meta:
        return [], []

    name = str(data.get("name") or plugin_meta.get("id") or package_dir.name)
    version = str(data.get("version") or plugin_meta.get("version") or "")
    description = str(plugin_meta.get("description") or data.get("description") or "")
    source_url = repository_url(data.get("repository") or data.get("homepage") or plugin_meta.get("repository"))
    origin = dsh_origin_from_path(package_dir)
    status = "enabled" if name in active_names else ("installed" if name in dependency_names else "cached")

    contributes: dict[str, Any] = {}
    if isinstance(dshx_section, dict) and isinstance(dshx_section.get("contributes"), dict):
        contributes = dshx_section["contributes"]
    elif isinstance(plugin_meta, dict) and isinstance(plugin_meta.get("contributes"), dict):
        contributes = plugin_meta["contributes"]
    declared_tools = contributes.get("tools") or []
    declared_skills = contributes.get("skills") or []
    capabilities = [str(item) for item in declared_tools if item] + [str(item) for item in declared_skills if item]
    declared_skill_paths: dict[str, Path] = {}
    raw_skill_paths = contributes.get("skillPaths") or {}
    if isinstance(raw_skill_paths, dict):
        package_root = package_dir.resolve(strict=False)
        for skill_name, relative_path in raw_skill_paths.items():
            if not skill_name or not isinstance(relative_path, str) or not relative_path.strip():
                continue
            candidate = (package_dir / relative_path).resolve(strict=False)
            if not path_is_within(candidate, package_root):
                diagnostics.append(f"DSH runtime skill path escapes plugin root: {relative_path}")
            elif not candidate.is_file():
                diagnostics.append(f"Missing DSH runtime skill metadata: {candidate}")
            else:
                declared_skill_paths[str(skill_name)] = candidate

    files = [manifest]
    if plugin_meta_path.is_file():
        files.append(plugin_meta_path)
    patch_file = package_dir / "cordis.patch.yml"
    if patch_file.is_file():
        files.append(patch_file)
    fingerprint = hash_metadata(files)
    plugin_id = f"dsh:plugin:{slug(name)}:{slug(origin)}"
    items: list[dict[str, Any]] = [{
        "id": plugin_id,
        "ecosystem": "dsh",
        "kind": "plugin",
        "name": name,
        "display_name": name,
        "description_original": description,
        "status": status,
        "version": version,
        "source_url": source_url,
        "path": str(package_dir.resolve(strict=False)),
        "origin": origin,
        "capabilities": capabilities,
        "fingerprint": fingerprint,
    }]

    local_skill_names: set[str] = set()
    skills_dir = package_dir / "skills"
    if skills_dir.is_dir():
        for skill_md in walk_named(skills_dir, "SKILL.md", max_depth=4):
            try:
                metadata = parse_frontmatter(read_limited(skill_md))
            except CatalogError as exc:
                diagnostics.append(str(exc))
                continue
            skill_name = str(metadata.get("name") or skill_md.parent.name)
            metadata_path = declared_skill_paths.get(skill_name, skill_md)
            if metadata_path != skill_md:
                try:
                    metadata = parse_frontmatter(read_limited(metadata_path))
                except CatalogError as exc:
                    diagnostics.append(str(exc))
                    continue
            local_skill_names.add(skill_name)
            items.append({
                "id": f"dsh:plugin-skill:{slug(skill_name)}:{slug(name)}:{slug(origin)}",
                "ecosystem": "dsh",
                "kind": "plugin_skill",
                "name": skill_name,
                "display_name": skill_name,
                "description_original": metadata.get("description", ""),
                "status": status,
                "version": version,
                "source_url": source_url,
                "path": str(metadata_path.parent.resolve(strict=False)),
                "origin": f"{name}@{origin}",
                "parent_plugin": plugin_id,
                "fingerprint": hash_metadata([metadata_path]),
            })
    for skill_name in declared_skills:
        skill_name = str(skill_name) if skill_name else ""
        if not skill_name or skill_name in local_skill_names:
            continue
        metadata_path = declared_skill_paths.get(skill_name)
        if metadata_path is not None:
            try:
                metadata = parse_frontmatter(read_limited(metadata_path))
            except CatalogError as exc:
                diagnostics.append(str(exc))
                continue
            items.append({
                "id": f"dsh:plugin-skill:{slug(skill_name)}:{slug(name)}:{slug(origin)}",
                "ecosystem": "dsh",
                "kind": "plugin_skill",
                "name": skill_name,
                "display_name": skill_name,
                "description_original": metadata.get("description", ""),
                "status": status,
                "version": version,
                "source_url": source_url,
                "path": str(metadata_path.parent.resolve(strict=False)),
                "origin": f"{name}@{origin}",
                "parent_plugin": plugin_id,
                "fingerprint": hash_metadata([metadata_path]),
            })
            continue
        items.append({
            "id": f"dsh:plugin-skill:{slug(skill_name)}:{slug(name)}:{slug(origin)}",
            "ecosystem": "dsh",
            "kind": "plugin_skill",
            "name": skill_name,
            "display_name": skill_name,
            "description_original": f"Declared by plugin {name}.",
            "status": status,
            "version": version,
            "source_url": source_url,
            "path": str(package_dir.resolve(strict=False)),
            "path_is_virtual": True,
            "origin": f"{name}@{origin}",
            "parent_plugin": plugin_id,
            "fingerprint": fingerprint,
        })
    return items, diagnostics


def scan_skill_file(
    skill_md: Path,
    root: Path,
    alias: str,
    include_system: bool,
    ecosystem: str = "codex",
) -> tuple[dict[str, Any] | None, list[str]]:
    diagnostics: list[str] = []
    try:
        rel = skill_md.relative_to(root)
    except ValueError:
        return None, [f"Out-of-root skill ignored: {skill_md}"]
    builtin = bool(rel.parts and rel.parts[0] == ".system")
    if builtin and not include_system:
        return None, []
    try:
        metadata = parse_frontmatter(read_limited(skill_md))
    except CatalogError as exc:
        return None, [str(exc)]
    name = metadata.get("name") or skill_md.parent.name
    if "name" not in metadata:
        diagnostics.append(f"Missing frontmatter name: {skill_md}")
    openai_path = skill_md.parent / "agents" / "openai.yaml"
    interface: dict[str, str] = {}
    files = [skill_md]
    if openai_path.is_file():
        files.append(openai_path)
        with contextlib.suppress(CatalogError):
            interface = parse_openai_yaml(read_limited(openai_path))
    kind = "system_skill" if builtin else "skill"
    item = {
        "id": f"{ecosystem}:{kind}:{slug(name)}:{alias}",
        "ecosystem": ecosystem,
        "kind": kind,
        "name": name,
        "display_name": interface.get("display_name") or name,
        "description_original": metadata.get("description", ""),
        "status": "builtin" if builtin else "installed",
        "version": None,
        "source_url": None,
        "path": str(skill_md.parent.resolve(strict=False)),
        "origin": alias,
        "fingerprint": hash_metadata(files),
    }
    return item, diagnostics


def infer_plugin_origin(manifest: Path, root: Path) -> tuple[str, str]:
    try:
        rel = manifest.relative_to(root)
        parts = rel.parts
    except ValueError:
        parts = manifest.parts
    if "cache" in parts:
        index = parts.index("cache")
        if len(parts) > index + 2:
            return parts[index + 1], parts[index + 2]
    if len(parts) >= 4:
        return parts[-4], parts[-3]
    return "unknown-market", manifest.parents[1].name


def version_key(value: Any) -> tuple[Any, ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in re.split(r"[._-]", str(value or "0"))
    )


def scan_plugin_manifest(
    manifest: Path,
    root: Path,
    alias: str,
    enabled_plugins: dict[str, bool],
) -> tuple[list[dict[str, Any]], list[str]]:
    diagnostics: list[str] = []
    try:
        data = json.loads(read_limited(manifest))
    except (CatalogError, json.JSONDecodeError) as exc:
        return [], [f"Invalid plugin manifest {manifest}: {exc}"]
    if not isinstance(data, dict):
        return [], [f"Plugin manifest is not an object: {manifest}"]
    market, inferred_name = infer_plugin_origin(manifest, root)
    name = str(data.get("name") or inferred_name)
    plugin_key = f"{name}@{market}".lower()
    enabled = enabled_plugins.get(plugin_key, False)
    source = normalize_url(data.get("repository") or data.get("homepage"))
    interface = data.get("interface") if isinstance(data.get("interface"), dict) else {}
    plugin_id = f"codex:plugin:{slug(name)}:{slug(market)}"
    files = [manifest]
    items: list[dict[str, Any]] = [{
        "id": plugin_id,
        "ecosystem": "codex",
        "kind": "plugin",
        "name": name,
        "display_name": interface.get("displayName") or name,
        "description_original": interface.get("longDescription") or data.get("description") or "",
        "status": "enabled" if enabled else "cached",
        "version": str(data.get("version") or ""),
        "source_url": source,
        "path": str(manifest.parent.parent.resolve(strict=False)),
        "origin": market,
        "capabilities": interface.get("capabilities") or [],
        "fingerprint": hash_metadata(files),
    }]
    skill_root_value = data.get("skills")
    if isinstance(skill_root_value, str):
        plugin_base = manifest.parent.parent
        skill_root = (plugin_base / skill_root_value).resolve(strict=False)
        if path_is_within(skill_root, plugin_base.resolve(strict=False)) and skill_root.is_dir():
            for skill_md in walk_named(skill_root, "SKILL.md", max_depth=5):
                try:
                    metadata = parse_frontmatter(read_limited(skill_md))
                except CatalogError as exc:
                    diagnostics.append(str(exc))
                    continue
                skill_name = metadata.get("name") or skill_md.parent.name
                openai_path = skill_md.parent / "agents" / "openai.yaml"
                child_files = [skill_md]
                display_name = skill_name
                if openai_path.is_file():
                    child_files.append(openai_path)
                    with contextlib.suppress(CatalogError):
                        display_name = parse_openai_yaml(read_limited(openai_path)).get("display_name", skill_name)
                items.append({
                    "id": f"codex:plugin-skill:{slug(skill_name)}:{slug(name)}:{slug(market)}",
                    "ecosystem": "codex",
                    "kind": "plugin_skill",
                    "name": skill_name,
                    "display_name": display_name,
                    "description_original": metadata.get("description", ""),
                    "status": "enabled" if enabled else "cached",
                    "version": str(data.get("version") or ""),
                    "source_url": source,
                    "path": str(skill_md.parent.resolve(strict=False)),
                    "origin": f"{name}@{market}",
                    "parent_plugin": plugin_id,
                    "fingerprint": hash_metadata(child_files),
                })
        else:
            diagnostics.append(f"Plugin skills path escapes plugin root: {manifest}")
    return items, diagnostics


def profile_from_args(config: dict[str, Any], args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    name = args.profile or config.get("active_profile", "default")
    profile = dict(config.get("profiles", {}).get(name, {}))
    if not profile and args.command not in {"configure", "discover", "inspect-link", "resolve-installer"}:
        raise CatalogError(f"Profile '{name}' is not configured; run discover then configure")
    if getattr(args, "skill_root", None):
        profile["skill_roots"] = normalize_root_entries(args.skill_root, "skills")
    if getattr(args, "plugin_root", None):
        profile["plugin_roots"] = normalize_root_entries(args.plugin_root, "plugins")
    if getattr(args, "catalog", None):
        profile["catalog_path"] = str(canonical_path(args.catalog))
    if getattr(args, "include_system", None) is not None:
        profile["include_system"] = bool(args.include_system)
    if getattr(args, "dsh_profile", None):
        profile["dsh_profile"] = args.dsh_profile
    if getattr(args, "scan_depth", None) is not None:
        profile["scan_depth"] = args.scan_depth
    return name, profile


def finalize_inventory(
    items: list[dict[str, Any]],
    diagnostics: list[str],
    profile_name: str,
) -> dict[str, Any]:
    path_deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        item_path = item.get("path")
        if (
            item_path
            and not item.get("path_is_virtual")
            and item.get("kind") in {"skill", "system_skill", "plugin_skill", "plugin"}
        ):
            path_key = os.path.normcase(str(canonical_path(item_path)))
            key = (str(item.get("kind")), path_key)
            if key in path_deduped:
                diagnostics.append(f"Duplicate capability path collapsed: {item_path}")
                continue
            path_deduped[key] = item
        else:
            path_deduped[(str(item.get("kind")), str(item.get("id")))] = item

    deduped: dict[str, dict[str, Any]] = {}
    for item in path_deduped.values():
        previous = deduped.get(item["id"])
        if previous is None:
            deduped[item["id"]] = item
            continue
        preferred = item
        if previous.get("status") == "enabled" and item.get("status") != "enabled":
            preferred = previous
        elif previous.get("status") == item.get("status") and version_key(previous.get("version")) > version_key(item.get("version")):
            preferred = previous
        deduped[item["id"]] = preferred
        diagnostics.append(f"Duplicate capability collapsed: {item['id']}")
    ordered = sorted(deduped.values(), key=lambda item: (item["kind"], item["name"].lower(), item["id"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "profile": profile_name,
        "scanned_at": utc_now(),
        "items": ordered,
        "diagnostics": sorted(set(diagnostics)),
    }


def scan_codex_profile(profile_name: str, profile: dict[str, Any]) -> dict[str, Any]:
    include_system = bool(profile.get("include_system", False))
    scan_depth = int(profile.get("scan_depth", MAX_SCAN_DEPTH))
    if scan_depth < 0:
        raise CatalogError("scan_depth must be zero or greater")
    skill_roots = normalize_root_entries(profile.get("skill_roots", []), "skills")
    plugin_roots = normalize_root_entries(profile.get("plugin_roots", []), "plugins")
    config_value = profile.get("codex_config")
    codex_config = canonical_path(config_value) if config_value else None
    enabled_plugins, marketplaces, diagnostics = read_codex_config(codex_config)
    items: list[dict[str, Any]] = []
    for entry in skill_roots:
        root = canonical_path(entry["path"])
        if not root.is_dir():
            diagnostics.append(f"Missing skill root: {root}")
            continue
        for skill_md in walk_named(root, "SKILL.md", max_depth=scan_depth):
            item, item_diagnostics = scan_skill_file(skill_md, root, entry["alias"], include_system, ecosystem="codex")
            diagnostics.extend(item_diagnostics)
            if item:
                items.append(item)
    for entry in plugin_roots:
        root = canonical_path(entry["path"])
        if not root.is_dir():
            continue
        for manifest in walk_named(root, "plugin.json", max_depth=scan_depth):
            if manifest.parent.name != ".codex-plugin":
                continue
            plugin_items, item_diagnostics = scan_plugin_manifest(
                manifest, root, entry["alias"], enabled_plugins
            )
            diagnostics.extend(item_diagnostics)
            items.extend(plugin_items)
    for name, data in marketplaces.items():
        source = data.get("source") if isinstance(data, dict) else None
        items.append({
            "id": f"codex:marketplace:{slug(str(name))}",
            "ecosystem": "codex",
            "kind": "marketplace",
            "name": str(name),
            "display_name": str(name),
            "description_original": "",
            "status": "configured",
            "version": None,
            "source_url": normalize_url(source) if isinstance(source, str) and source.startswith(("http://", "https://")) else None,
            "path": str(source or ""),
            "origin": "codex-config",
            "fingerprint": hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest(),
        })
    return finalize_inventory(items, diagnostics, profile_name)


def scan_dsh_profile(profile_name: str, profile: dict[str, Any]) -> dict[str, Any]:
    include_system = bool(profile.get("include_system", False))
    scan_depth = int(profile.get("scan_depth", MAX_SCAN_DEPTH))
    if scan_depth < 0:
        raise CatalogError("scan_depth must be zero or greater")
    skill_roots = normalize_root_entries(profile.get("skill_roots", []), "skills")
    plugin_roots = normalize_root_entries(profile.get("plugin_roots", []), "plugins")
    home = dsh_home_dir(profile.get("dsh_home"))
    dsh_profile = str(profile.get("dsh_profile") or "web")
    manifest_path = canonical_path(profile.get("dsh_config")) if profile.get("dsh_config") else dsh_profile_dir(home, dsh_profile) / "package.json"
    patch_path = canonical_path(profile.get("dsh_patch")) if profile.get("dsh_patch") else dsh_profile_dir(home, dsh_profile) / "cordis.patch.yml"
    bundles, dependencies, diagnostics = read_dsh_profile_manifest(manifest_path)
    patch_names = read_dsh_patch_names(patch_path)
    selected_state = (set(bundles) | patch_names, dependencies)
    profile_states = collect_dsh_profile_states(home)
    profile_states.setdefault(dsh_profile, selected_state)
    items: list[dict[str, Any]] = []
    for entry in skill_roots:
        root = canonical_path(entry["path"])
        if not root.is_dir():
            diagnostics.append(f"Missing skill root: {root}")
            continue
        for skill_md in walk_named(root, "SKILL.md", max_depth=scan_depth):
            item, item_diagnostics = scan_skill_file(skill_md, root, entry["alias"], include_system, ecosystem="dsh")
            diagnostics.extend(item_diagnostics)
            if item:
                items.append(item)
    for entry in plugin_roots:
        root = canonical_path(entry["path"])
        if not root.is_dir():
            diagnostics.append(f"Missing plugin root: {root}")
            continue
        for package_dir in iter_top_level_dsh_packages(root):
            origin = dsh_origin_from_path(package_dir)
            active_names, dependency_names = profile_states.get(origin, selected_state)
            plugin_items, item_diagnostics = scan_dsh_plugin_package(
                package_dir, entry["alias"], active_names, dependency_names
            )
            diagnostics.extend(item_diagnostics)
            items.extend(plugin_items)
    return finalize_inventory(items, diagnostics, profile_name)


def scan_profile(profile_name: str, profile: dict[str, Any]) -> dict[str, Any]:
    agent = profile.get("agent", "codex")
    if agent == "codex":
        return scan_codex_profile(profile_name, profile)
    if agent == "dsh":
        return scan_dsh_profile(profile_name, profile)
    raise CatalogError(f"Agent adapter '{agent}' is not implemented; supported agents: codex, dsh")


def compute_diff(inventory: dict[str, Any], previous_state: dict[str, Any] | None) -> dict[str, Any]:
    previous_items = (previous_state or {}).get("items", {})
    current = {item["id"]: item for item in inventory.get("items", [])}
    new = sorted(key for key in current if key not in previous_items or previous_items[key].get("status") == "removed")
    changed = sorted(
        key for key in current
        if key in previous_items
        and previous_items[key].get("status") != "removed"
        and (
            previous_items[key].get("fingerprint") != current[key].get("fingerprint")
            or previous_items[key].get("status") != current[key].get("status")
        )
    )
    removed = sorted(
        key for key, value in previous_items.items()
        if value.get("status") != "removed" and key not in current
    )
    unchanged = sorted(key for key in current if key not in set(new + changed))
    return {
        "profile": inventory.get("profile"),
        "new": new,
        "changed": changed,
        "removed": removed,
        "unchanged": unchanged,
        "diagnostics": inventory.get("diagnostics", []),
    }


def default_summary(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "name_zh": item.get("display_name") or item.get("name"),
        "scope_zh": "尚未撰写中文使用范围。",
        "effects_zh": "请根据权威来源补充可验证的能力效果。",
        "triggers_zh": [],
        "limitations_zh": [],
        "permissions_zh": [],
        "category_zh": infer_category(item, {}),
        "tags_zh": [],
        "aliases": [],
        "verification": "待核验",
        "evidence_urls": [item["source_url"]] if item.get("source_url") else [],
    }


def discovery_items(payload: Any) -> list[dict[str, Any]]:
    """Normalize user-confirmed marketplace or repository discoveries."""
    if not payload:
        return []
    repository = None
    if isinstance(payload, dict) and isinstance(payload.get("skills"), list):
        raw_items = payload["skills"]
        repository = payload.get("repository")
    elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
        raw_items = payload["items"]
    elif isinstance(payload, list):
        raw_items = payload
    else:
        raise CatalogError("Discoveries must be a list, an items list, or inspect-link output")
    result: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict) or not raw.get("name"):
            raise CatalogError("Every discovered item must be an object with a name")
        name = str(raw["name"])
        source_url = normalize_url(raw.get("source_url") or raw.get("install_url") or repository)
        source_key = f"{source_url or ''}|{raw.get('path', '')}|{name}"
        suffix = hashlib.sha256(source_key.encode()).hexdigest()[:10]
        result.append({
            "id": raw.get("id") or f"codex:discovered:{slug(name)}:{suffix}",
            "ecosystem": str(raw.get("ecosystem") or "codex"),
            "kind": "discovered",
            "name": name,
            "display_name": str(raw.get("display_name") or name),
            "description_original": str(raw.get("description_original") or raw.get("description") or ""),
            "status": "discovered",
            "version": raw.get("version"),
            "source_url": source_url,
            "path": str(raw.get("path") or ""),
            "origin": str(raw.get("origin") or "current-discovery"),
            "fingerprint": hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest(),
        })
    return result


def validate_summary(value: Any, item_id: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CatalogError(f"Summary for {item_id} must be an object")
    result = dict(value)
    verification = result.get("verification", "待核验")
    if verification not in VERIFICATION_VALUES:
        raise CatalogError(f"Invalid verification for {item_id}: {verification}")
    result["verification"] = verification
    for key in ("triggers_zh", "limitations_zh", "permissions_zh", "evidence_urls", "tags_zh", "aliases"):
        raw = result.get(key, [])
        if not isinstance(raw, list) or not all(isinstance(entry, str) for entry in raw):
            raise CatalogError(f"{key} for {item_id} must be a string list")
        result[key] = raw
    for key in ("name_zh", "scope_zh", "effects_zh", "category_zh"):
        if not isinstance(result.get(key, ""), str):
            raise CatalogError(f"{key} for {item_id} must be a string")
    return result


def infer_category(item: dict[str, Any], summary: dict[str, Any]) -> str:
    explicit = summary.get("category_zh")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    searchable = " ".join(str(value) for value in (
        item.get("name", ""),
        item.get("display_name", ""),
        item.get("description_original", ""),
        summary.get("name_zh", ""),
        summary.get("scope_zh", ""),
        summary.get("effects_zh", ""),
    )).lower()
    for category, keywords in CATEGORY_RULES:
        if any(keyword in searchable for keyword in keywords):
            return category
    return "其他"


def item_anchor(item: dict[str, Any]) -> str:
    digest = hashlib.sha256(str(item.get("id", "")).encode()).hexdigest()[:10]
    return f"cap-{slug(str(item.get('name', 'capability')))}-{digest}"


def markdown_cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def search_catalog(state: dict[str, Any], query: str, limit: int = 50, category: str | None = None) -> dict[str, Any]:
    terms = [term.lower() for term in re.split(r"\s+", query.strip()) if term]
    if not terms:
        raise CatalogError("search query must not be empty")
    matches: list[tuple[int, dict[str, Any]]] = []
    for item in state.get("items", {}).values():
        if item.get("status") == "removed":
            continue
        summary = item.get("summary") or default_summary(item)
        item_category = infer_category(item, summary)
        if category and item_category != category:
            continue
        weighted_fields = [
            (8, item.get("name", "")),
            (8, summary.get("name_zh", "")),
            (6, " ".join(summary.get("aliases", []))),
            (5, " ".join(summary.get("tags_zh", []))),
            (3, item.get("description_original", "")),
            (3, summary.get("scope_zh", "")),
            (3, summary.get("effects_zh", "")),
            (2, " ".join(summary.get("triggers_zh", []))),
            (1, item_category),
        ]
        lowered = [(weight, str(value).lower()) for weight, value in weighted_fields]
        if not all(any(term in value for _, value in lowered) for term in terms):
            continue
        score = sum(weight for term in terms for weight, value in lowered if term in value)
        result = {
            "id": item.get("id"),
            "name": item.get("name"),
            "name_zh": summary.get("name_zh") or item.get("display_name") or item.get("name"),
            "category_zh": item_category,
            "tags_zh": summary.get("tags_zh", []),
            "status": item.get("status"),
            "kind": item.get("kind"),
            "scope_zh": summary.get("scope_zh", ""),
            "path": item.get("path"),
            "anchor": item_anchor(item),
            "score": score,
        }
        matches.append((score, result))
    matches.sort(key=lambda pair: (-pair[0], str(pair[1]["name_zh"]).lower(), str(pair[1]["id"])))
    return {"query": query, "category": category, "total": len(matches), "matches": [item for _, item in matches[:limit]]}


SECTION_ORDER = [
    ("skill", "独立安装的 Skills"),
    ("system_skill", "内置系统 Skills"),
    ("plugin_skill", "插件附带 Skills"),
    ("plugin", "插件"),
    ("marketplace", "已配置市场"),
    ("discovered", "尚未安装的候选项"),
]


STATUS_ZH = {
    "builtin": "内置",
    "installed": "已安装",
    "enabled": "已启用",
    "cached": "仅缓存",
    "configured": "已配置",
    "discovered": "候选",
    "disabled": "已禁用",
}


def render_markdown(state: dict[str, Any]) -> str:
    lines = [
        "# 中文能力说明目录",
        "",
        f"> Profile：`{state.get('profile', 'default')}` · 最近更新：{state.get('generated_at', '')}",
        "",
        "本目录由 `catalog-capabilities-zh` 根据本地元数据和明确来源生成。`待核验` 条目不应视为已确认能力。",
        "",
    ]
    active_items = [item for item in state.get("items", {}).values() if item.get("status") != "removed"]
    if active_items:
        indexed: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
        counts: dict[str, int] = {}
        for item in active_items:
            summary = item.get("summary") or default_summary(item)
            category = infer_category(item, summary)
            name_zh = summary.get("name_zh") or item.get("display_name") or item.get("name")
            counts[category] = counts.get(category, 0) + 1
            indexed.append((category, str(name_zh), item, summary))
        category_rank = {name: index for index, name in enumerate(CATEGORY_ORDER)}
        indexed.sort(key=lambda row: (category_rank.get(row[0], len(CATEGORY_ORDER)), row[0], row[1].lower(), row[2].get("id", "")))
        count_text = " · ".join(
            f"{category} {counts[category]}"
            for category in CATEGORY_ORDER
            if counts.get(category)
        )
        extra_categories = sorted(set(counts) - set(CATEGORY_ORDER))
        if extra_categories:
            count_text += "".join(f" · {category} {counts[category]}" for category in extra_categories)
        lines.extend([
            "## 快速索引",
            "",
            f"共 {len(active_items)} 项。{count_text}",
            "",
            "可运行 `catalog_capabilities.py search <关键词>` 搜索中文名称、原始标识、别名、标签、范围和触发词。",
            "",
            "| 分类 | 中文名称 | 原始标识 | 状态 | 标签 |",
            "|---|---|---|---|---|",
        ])
        for category, name_zh, item, summary in indexed:
            tags = "、".join(summary.get("tags_zh", [])) or "—"
            lines.append(
                f"| {markdown_cell(category)} | [{markdown_cell(name_zh)}](#{item_anchor(item)}) | "
                f"`{markdown_cell(item.get('name'))}` | {STATUS_ZH.get(item.get('status'), item.get('status', '未知'))} | {markdown_cell(tags)} |"
            )
        lines.append("")
    for kind, title in SECTION_ORDER:
        section = sorted(
            (item for item in active_items if item.get("kind") == kind),
            key=lambda item: (item.get("name", "").lower(), item.get("id", "")),
        )
        if not section:
            continue
        lines.extend([f"## {title}", ""])
        for item in section:
            summary = item.get("summary") or default_summary(item)
            lines.extend([
                f"<a id=\"{item_anchor(item)}\"></a>",
                "",
                f"### {summary.get('name_zh') or item.get('display_name') or item.get('name')}",
                "",
                f"- 原始标识：`{item.get('name', '')}`",
                f"- 状态：{STATUS_ZH.get(item.get('status'), item.get('status', '未知'))}",
                f"- 类型：`{item.get('kind', '')}` · 生态：`{item.get('ecosystem', '')}`",
                f"- 功能分类：{infer_category(item, summary)}",
                f"- 使用范围：{summary.get('scope_zh', '')}",
                f"- 能力效果：{summary.get('effects_zh', '')}",
                f"- 核验状态：{summary.get('verification', '待核验')}",
            ])
            if item.get("version"):
                lines.append(f"- 版本：`{item['version']}`")
            if summary.get("triggers_zh"):
                lines.append("- 典型触发：" + "；".join(summary["triggers_zh"]))
            if summary.get("tags_zh"):
                lines.append("- 搜索标签：" + "、".join(summary["tags_zh"]))
            if summary.get("aliases"):
                lines.append("- 搜索别名：" + "、".join(summary["aliases"]))
            if summary.get("limitations_zh"):
                lines.append("- 限制：" + "；".join(summary["limitations_zh"]))
            if summary.get("permissions_zh"):
                lines.append("- 权限与依赖：" + "；".join(summary["permissions_zh"]))
            evidence = summary.get("evidence_urls", [])
            if evidence:
                links = ", ".join(f"[{index + 1}]({url})" for index, url in enumerate(evidence))
                lines.append(f"- 来源：{links}")
            elif item.get("path"):
                lines.append(f"- 本地来源：`{item['path']}`")
            lines.extend([f"- 最后核验：{item.get('reviewed_at', state.get('generated_at', ''))}", ""])
    if len(lines) <= 6:
        lines.extend(["当前 profile 尚未发现任何能力。", ""])
    return "\n".join(lines)


def merge_state(
    inventory: dict[str, Any],
    previous: dict[str, Any] | None,
    summaries: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    previous = previous or {"schema_version": SCHEMA_VERSION, "items": {}}
    old_items = previous.get("items", {})
    supplied = summaries.get("items", summaries)
    if not isinstance(supplied, dict):
        raise CatalogError("Summaries must be an object or contain an 'items' object")
    current: dict[str, dict[str, Any]] = {}
    now = utc_now()
    for raw in inventory.get("items", []):
        item = dict(raw)
        old = old_items.get(item["id"], {})
        if item["id"] in supplied:
            item["summary"] = validate_summary(supplied[item["id"]], item["id"])
            item["reviewed_at"] = now
        elif old.get("summary"):
            item["summary"] = old["summary"]
            item["reviewed_at"] = old.get("reviewed_at")
            if old.get("fingerprint") != item.get("fingerprint"):
                item["summary"] = dict(item["summary"])
                item["summary"]["verification"] = "待核验"
        else:
            item["summary"] = default_summary(item)
            item["reviewed_at"] = None
        current[item["id"]] = item
    unknown_summaries = sorted(set(supplied) - set(current))
    if unknown_summaries:
        raise CatalogError("Summary IDs are not present in the inventory: " + ", ".join(unknown_summaries))
    for item_id, old in old_items.items():
        if item_id not in current:
            if old.get("kind") == "discovered" and old.get("status") == "discovered":
                current[item_id] = dict(old)
                continue
            tombstone = dict(old)
            tombstone["status"] = "removed"
            tombstone["removed_at"] = old.get("removed_at") or now
            current[item_id] = tombstone
    comparable_old = {"profile": previous.get("profile"), "items": old_items}
    comparable_new = {"profile": inventory.get("profile"), "items": current}
    changed = comparable_old != comparable_new
    generated_at = now if changed else previous.get("generated_at", now)
    state = {
        "schema_version": SCHEMA_VERSION,
        "profile": inventory.get("profile"),
        "generated_at": generated_at,
        "items": current,
    }
    return state, compute_diff(inventory, previous), changed


def parse_github_url(url: str) -> dict[str, str | None]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        raise CatalogError("inspect-link accepts only public https://github.com URLs")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        raise CatalogError("GitHub URL must identify owner/repository")
    owner, repo = parts[0], parts[1].removesuffix(".git")
    ref: str | None = None
    prefix = ""
    if len(parts) >= 4 and parts[2] == "tree":
        ref = parts[3]
        prefix = "/".join(parts[4:])
    return {"owner": owner, "repo": repo, "ref": ref, "prefix": prefix}


def fetch_json(url: str, *, limit: int = MAX_REMOTE_BYTES) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "catalog-capabilities-zh/1"})
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read(limit + 1)
    if len(raw) > limit:
        raise CatalogError(f"Remote response exceeds {limit} bytes")
    return json.loads(raw.decode("utf-8"))


def fetch_text(url: str, *, limit: int = MAX_REMOTE_BYTES) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "catalog-capabilities-zh/1"})
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read(limit + 1)
    if len(raw) > limit:
        raise CatalogError(f"Remote response exceeds {limit} bytes")
    return raw.decode("utf-8-sig", errors="replace")


def inspect_github_repository(
    url: str,
    json_fetcher: Callable[[str], Any] = fetch_json,
    text_fetcher: Callable[[str], str] = fetch_text,
) -> dict[str, Any]:
    target = parse_github_url(url)
    owner, repo = target["owner"], target["repo"]
    api_base = f"https://api.github.com/repos/{owner}/{repo}"
    try:
        repo_data = json_fetcher(api_base)
        ref = target["ref"] or repo_data.get("default_branch", "main")
        tree = json_fetcher(f"{api_base}/git/trees/{urllib.parse.quote(str(ref), safe='')}?recursive=1")
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        raise CatalogError(f"Cannot inspect public GitHub repository: {exc}") from exc
    prefix = str(target["prefix"] or "").strip("/")
    paths = [
        entry.get("path") for entry in tree.get("tree", [])
        if isinstance(entry, dict)
        and entry.get("type") == "blob"
        and str(entry.get("path", "")).endswith("SKILL.md")
        and (not prefix or str(entry.get("path", "")).startswith(prefix + "/") or entry.get("path") == prefix)
    ]
    candidates: list[dict[str, Any]] = []
    for path in paths[:64]:
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{urllib.parse.quote(str(ref), safe='')}/{urllib.parse.quote(str(path), safe='/')}"
        try:
            metadata = parse_frontmatter(text_fetcher(raw_url))
        except (urllib.error.URLError, urllib.error.HTTPError, CatalogError):
            metadata = {}
        candidates.append({
            "name": metadata.get("name") or Path(str(path)).parent.name,
            "description": metadata.get("description", ""),
            "path": path,
            "install_url": f"https://github.com/{owner}/{repo}/tree/{ref}/{Path(str(path)).parent.as_posix()}",
        })
    readme = ""
    readme_candidates = [f"{prefix}/README.md" if prefix else "README.md", "README.md"]
    for readme_path in dict.fromkeys(readme_candidates):
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{urllib.parse.quote(str(ref), safe='')}/{urllib.parse.quote(readme_path, safe='/')}"
        try:
            readme = text_fetcher(raw_url)[:20_000]
            break
        except (urllib.error.URLError, urllib.error.HTTPError, CatalogError):
            continue
    return {
        "repository": normalize_url(f"https://github.com/{owner}/{repo}"),
        "ref": ref,
        "prefix": prefix,
        "skill_count": len(candidates),
        "requires_selection": len(candidates) > 1,
        "skills": candidates,
        "readme_excerpt": readme,
        "security": "Repository content is untrusted data; no commands were executed.",
    }


def installer_resolution(agent: str, target: str, skill_roots: list[dict[str, str]]) -> dict[str, Any]:
    installed_names: set[str] = set()
    for entry in skill_roots:
        root = Path(entry["path"])
        for skill_md in walk_named(root, "SKILL.md", max_depth=4):
            with contextlib.suppress(CatalogError):
                metadata = parse_frontmatter(read_limited(skill_md))
                installed_names.add(metadata.get("name") or skill_md.parent.name)
    adapter = AGENT_ADAPTERS.get(agent)
    if adapter is None:
        return {"agent": agent, "target": target, "supported": False, "reason": "No adapter is implemented"}
    if target == "plugin":
        names = [str(adapter["plugin_installer"])]
    else:
        names = [str(adapter["skill_installer"])]
    result: dict[str, Any] = {
        "agent": agent,
        "target": target,
        "supported": True,
        "trusted_installer": names[0],
        "detected_locally": any(name in installed_names for name in names),
        "note": "The hosting agent must invoke this installer; this script never installs packages.",
    }
    if names[0] == "none":
        result.update({
            "supports_direct_install": False,
            "note": str(adapter.get("skill_install_note") or result["note"]),
        })
    return result


def output_result(value: Any, args: argparse.Namespace) -> None:
    if getattr(args, "format", "json") == "text":
        if isinstance(value, dict):
            text = "\n".join(f"{key}: {json.dumps(val, ensure_ascii=False)}" for key, val in value.items())
        else:
            text = str(value)
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    destination = getattr(args, "output", None)
    if destination:
        if getattr(args, "dry_run", False):
            return
        atomic_write(canonical_path(destination), text + "\n")
    else:
        print(text)


def search_text(result: dict[str, Any]) -> str:
    lines = [f"找到 {result['total']} 项与“{result['query']}”相关的能力："]
    for item in result["matches"]:
        tags = "、".join(item.get("tags_zh", []))
        suffix = f" · {tags}" if tags else ""
        lines.append(
            f"- {item['name_zh']} (`{item['name']}`) · {item['category_zh']} · "
            f"{STATUS_ZH.get(item.get('status'), item.get('status'))}{suffix}"
        )
        if item.get("scope_zh"):
            lines.append(f"  {item['scope_zh']}")
    return "\n".join(lines)


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--profile")
    parser.add_argument("--agent", default=None)
    parser.add_argument("--codex-home")
    parser.add_argument("--dsh-home")
    parser.add_argument("--dsh-profile", default=None, help="target DSH profile name (default web)")
    parser.add_argument("--skill-root", action="append")
    parser.add_argument("--linked-root", action="append", help="confirm a directory link as an independent skill root")
    parser.add_argument("--plugin-root", action="append")
    parser.add_argument("--catalog")
    system_group = parser.add_mutually_exclusive_group()
    system_group.add_argument("--include-system", action="store_true", dest="include_system", default=None)
    system_group.add_argument("--exclude-system", action="store_false", dest="include_system")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    parser.add_argument("--output")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--scan-depth", type=int)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("discover", "discover candidate capability roots"),
        ("configure", "save a confirmed local profile"),
        ("scan", "scan configured capability metadata"),
        ("diff", "compare a scan with saved state"),
        ("render", "merge summaries and render the Chinese catalog"),
        ("status", "show profile and catalog status"),
        ("search", "search the current capability catalog"),
        ("resolve-installer", "describe the trusted installer for this agent"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        add_common(command)
    inspect_link = subparsers.add_parser("inspect-link", help="inspect a public GitHub repository without installing")
    add_common(inspect_link)
    inspect_link.add_argument("--url", required=True)
    discover_links = subparsers.add_parser("discover-links", help="list linked skill roots beneath a grouping directory")
    add_common(discover_links)
    discover_links.add_argument("--root", required=True)
    discover_links.add_argument("--link-depth", type=int, default=3)
    render = subparsers.choices["render"]
    render.add_argument("--summaries", type=Path, required=True)
    render.add_argument("--discoveries", type=Path)
    resolver = subparsers.choices["resolve-installer"]
    resolver.add_argument("--target", choices=("skill", "plugin"), default="skill")
    search = subparsers.choices["search"]
    search.add_argument("query")
    search.add_argument("--category")
    search.add_argument("--limit", type=int, default=50)
    return parser


def command_configure(args: argparse.Namespace, config_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    profile_name = args.profile or config.get("active_profile", "default")
    agent = str(args.agent or ("dsh" if args.dsh_home else "codex"))
    if agent == "dsh":
        home = dsh_home_dir(args.dsh_home)
        dsh_profile = str(args.dsh_profile or "web")
        skill_values: list[Any] = list(args.skill_root or [str(home / "skills")])
        plugin_values: list[str] = list(args.plugin_root or [str(root) for root, _ in dsh_plugin_roots(home, dsh_profile)])
        catalog = canonical_path(args.catalog or home / "CAPABILITIES.zh-CN.md")
    else:
        codex_home = canonical_path(args.codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        skill_values = list(args.skill_root or [str(codex_home / "skills")])
        plugin_values = list(args.plugin_root or [str(codex_home / "plugins")])
        catalog = canonical_path(args.catalog or codex_home / "CAPABILITIES.zh-CN.md")
    for raw_link in args.linked_root or []:
        source_link = absolute_path(raw_link)
        if not is_directory_link(source_link):
            raise CatalogError(f"--linked-root must identify a directory symlink or junction: {source_link}")
        if not source_link.resolve(strict=False).is_dir():
            raise CatalogError(f"Linked skill root target is missing: {source_link}")
        skill_values.append({
            "path": str(source_link),
            "alias": f"linked-{source_link.name}",
            "group": source_link.name,
            "source_link": str(source_link),
            "source": "linked-root",
            "confirmed": True,
        })
    if args.scan_depth is not None and args.scan_depth < 0:
        raise CatalogError("--scan-depth must be zero or greater")
    profile: dict[str, Any] = {
        "agent": agent,
        "skill_roots": normalize_root_entries(skill_values, "skills"),
        "plugin_roots": normalize_root_entries(plugin_values, "plugins"),
        "catalog_path": str(catalog),
        "include_system": bool(args.include_system),
        "scan_depth": args.scan_depth if args.scan_depth is not None else MAX_SCAN_DEPTH,
        "configured_at": utc_now(),
    }
    if agent == "dsh":
        profile.update({
            "dsh_home": str(home),
            "dsh_profile": dsh_profile,
            "dsh_config": str(dsh_profile_dir(home, dsh_profile) / "package.json"),
            "dsh_patch": str(dsh_profile_dir(home, dsh_profile) / "cordis.patch.yml"),
        })
    else:
        profile["codex_config"] = str(codex_home / "config.toml")
    updated = dict(config)
    updated["profiles"] = dict(config.get("profiles", {}))
    updated["profiles"][profile_name] = profile
    updated["active_profile"] = profile_name
    if not args.dry_run:
        lock = config_path.with_name(config_path.name + ".lock")
        with FileLock(lock):
            atomic_write(config_path, json.dumps(updated, ensure_ascii=False, indent=2, sort_keys=True) + "\n", backup=True)
    return {"configured": not args.dry_run, "dry_run": args.dry_run, "config_path": str(config_path), "profile": profile_name, "settings": profile}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(OSError, ValueError):
                reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    config_path = canonical_path(args.config)
    try:
        if args.command == "inspect-link":
            output_result(inspect_github_repository(args.url), args)
            return 0
        if args.command == "discover-links":
            output_result(
                discover_linked_roots(
                    Path(args.root),
                    max_depth=args.link_depth,
                    scan_depth=args.scan_depth if args.scan_depth is not None else MAX_SCAN_DEPTH,
                ),
                args,
            )
            return 0
        if args.command == "discover":
            output_result(discover_paths(config_path, args.codex_home, args.dsh_home), args)
            return 0
        config = load_config(config_path)
        if args.command == "configure":
            output_result(command_configure(args, config_path, config), args)
            return 0
        profile_name, profile = profile_from_args(config, args)
        if args.agent:
            profile["agent"] = args.agent
        if args.codex_home:
            home = canonical_path(args.codex_home)
            if not args.skill_root:
                profile["skill_roots"] = normalize_root_entries([home / "skills"], "skills")
            if not args.plugin_root:
                profile["plugin_roots"] = normalize_root_entries([home / "plugins"], "plugins")
            profile["codex_config"] = str(home / "config.toml")
        if args.dsh_home:
            home = dsh_home_dir(args.dsh_home)
            dsh_profile = str(args.dsh_profile or profile.get("dsh_profile") or "web")
            if not args.skill_root:
                profile["skill_roots"] = normalize_root_entries([home / "skills"], "skills")
            if not args.plugin_root:
                profile["plugin_roots"] = normalize_root_entries(
                    [root for root, _ in dsh_plugin_roots(home, dsh_profile)], "plugins"
                )
            profile.update({
                "agent": "dsh",
                "dsh_home": str(home),
                "dsh_profile": dsh_profile,
                "dsh_config": str(dsh_profile_dir(home, dsh_profile) / "package.json"),
                "dsh_patch": str(dsh_profile_dir(home, dsh_profile) / "cordis.patch.yml"),
            })
        elif args.dsh_profile:
            home = dsh_home_dir(profile.get("dsh_home"))
            dsh_profile = str(args.dsh_profile)
            profile.update({
                "dsh_home": str(home),
                "dsh_profile": dsh_profile,
                "dsh_config": str(dsh_profile_dir(home, dsh_profile) / "package.json"),
                "dsh_patch": str(dsh_profile_dir(home, dsh_profile) / "cordis.patch.yml"),
            })
        state_file = state_path(config_path, profile_name)
        previous_state = json_load(state_file, None)
        if args.command == "status":
            result = {
                "configured": bool(profile),
                "config_path": str(config_path),
                "profile": profile_name,
                "settings": profile,
                "state_path": str(state_file),
                "last_generated": (previous_state or {}).get("generated_at"),
            }
        elif args.command == "resolve-installer":
            result = installer_resolution(
                profile.get("agent", "codex"),
                args.target,
                normalize_root_entries(profile.get("skill_roots", []), "skills"),
            )
        else:
            inventory = scan_profile(profile_name, profile)
            if args.command == "scan":
                result = inventory
            elif args.command == "diff":
                result = compute_diff(inventory, previous_state)
            elif args.command == "search":
                searchable_state, _, _ = merge_state(inventory, previous_state, {})
                result = search_catalog(searchable_state, args.query, limit=max(1, args.limit), category=args.category)
                if args.format == "text":
                    result = search_text(result)
            elif args.command == "render":
                summaries = json_load(canonical_path(args.summaries), {})
                if args.discoveries:
                    discovered_payload = json_load(canonical_path(args.discoveries), {})
                    inventory["items"].extend(discovery_items(discovered_payload))
                    inventory["items"] = sorted(
                        {item["id"]: item for item in inventory["items"]}.values(),
                        key=lambda item: (item["kind"], item["name"].lower(), item["id"]),
                    )
                new_state, diff, changed = merge_state(inventory, previous_state, summaries)
                catalog_path = canonical_path(profile.get("catalog_path") or args.catalog or Path.home() / "CAPABILITIES.zh-CN.md")
                markdown = render_markdown(new_state)
                catalog_needs_write = not catalog_path.exists()
                if catalog_path.exists():
                    with contextlib.suppress(CatalogError):
                        catalog_needs_write = read_limited(catalog_path, 20_000_000) != markdown
                if not args.dry_run and (changed or catalog_needs_write):
                    lock_path = config_path.parent / f"catalog.{slug(profile_name)}.lock"
                    with FileLock(lock_path):
                        if catalog_needs_write:
                            atomic_write(catalog_path, markdown, backup=True)
                        if changed or not state_file.exists():
                            atomic_write(state_file, json.dumps(new_state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", backup=True)
                result = {
                    "changed": changed,
                    "written": (changed or catalog_needs_write) and not args.dry_run,
                    "dry_run": args.dry_run,
                    "catalog_path": str(catalog_path),
                    "state_path": str(state_file),
                    "diff": diff,
                    "unverified": sorted(
                        item_id for item_id, item in new_state["items"].items()
                        if item.get("status") != "removed" and item.get("summary", {}).get("verification") == "待核验"
                    ),
                }
            else:  # pragma: no cover - argparse guarantees a known command
                raise CatalogError(f"Unsupported command: {args.command}")
        output_result(result, args)
        return 0
    except CatalogError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        print(json.dumps({"error": f"Network inspection failed: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
