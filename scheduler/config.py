"""Configuration loading, safe path boundaries, and project folder inspection."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

EXCLUDED_DIRS = {
    ".git",
    ".svn",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "node_modules",
    ".vscode",
    ".idea",
    "dist",
    "build",
    ".pytest_cache",
}

PROJECT_SUBDIRS = {
    "python",
    "scripts",
    "src",
    "powershell",
    "ps1",
    "bat",
    "r",
}

DEFAULT_SCRIPT_EXTENSIONS = {
    ".py",
    ".ps1",
    ".bat",
    ".cmd",
    ".js",
    ".mjs",
    ".r",
    ".vbs",
}


def _default_python_exe() -> str:
    return sys.executable or "python"


def _default_powershell_exe() -> str:
    return "powershell.exe" if os.name == "nt" else "pwsh"


def server_timezone_name() -> str:
    """Return a readable local server timezone label with UTC offset."""
    now_local = datetime.now().astimezone()
    tz_name = now_local.tzname() or "Local"
    offset = now_local.utcoffset()
    if offset is None:
        return tz_name
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    offset_str = f"UTC{sign}{hours:02d}:{minutes:02d}"
    if tz_name.startswith("UTC"):
        return offset_str
    return f"{tz_name} ({offset_str})"


def _clamp_int(raw: dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    val = raw.get(key)
    if val is None or val == "":
        parsed = default
    else:
        parsed = int(val)
    return max(lo, min(hi, parsed))


def _normalize_script_runners(raw_runners: Any) -> dict[str, list[str]]:
    normalized: dict[str, list[str]] = {}
    if not isinstance(raw_runners, dict):
        return normalized
    for raw_ext, raw_cmd in raw_runners.items():
        ext = str(raw_ext).strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = "." + ext
        if isinstance(raw_cmd, list):
            tokens = [str(t).strip() for t in raw_cmd if str(t).strip()]
        elif isinstance(raw_cmd, str) and raw_cmd.strip():
            tokens = [raw_cmd.strip()]
        else:
            tokens = []
        if tokens:
            normalized[ext] = tokens
    return normalized


def _validate_rel_parts(parts: tuple[str, ...] | list[str]) -> None:
    for part in parts:
        if part in ("", ".", ".."):
            raise ValueError("Path traversal is not allowed.")
        if part.startswith("."):
            raise ValueError("Hidden files and folders are not allowed.")
        if part.lower() in EXCLUDED_DIRS:
            raise ValueError(f"Access to excluded folder '{part}' is not allowed.")


def _safe_is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _safe_is_file_within(path: Path, root_resolved: Path) -> bool:
    try:
        resolved = path.resolve()
        resolved.relative_to(root_resolved)
        return resolved.is_file()
    except (OSError, ValueError):
        return False


@dataclass(frozen=True)
class ScriptRoot:
    id: str
    label: str
    path: Path


@dataclass
class AppConfig:
    base_dir: Path
    app_name: str = "Analytics Scheduler"
    host: str = "127.0.0.1"
    port: int = 8787
    dev_mode: bool = False
    dev_user: str = "Analyst"
    iis_user_header: str = "X-IIS-User"
    python_exe: str = field(default_factory=_default_python_exe)
    powershell_exe: str = field(default_factory=_default_powershell_exe)
    script_runners: dict[str, list[str]] = field(default_factory=dict)
    max_concurrent_runs: int = 4
    default_timeout_minutes: int = 45
    log_retention_days: int = 30
    misfire_grace_minutes: int = 60
    max_log_mb: int = 25
    data_dir: Path = field(default_factory=lambda: Path("./data"))
    scripts_roots: list[ScriptRoot] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not (self.python_exe or "").strip():
            self.python_exe = _default_python_exe()
        if not (self.powershell_exe or "").strip():
            self.powershell_exe = _default_powershell_exe()
        self.script_runners = _normalize_script_runners(self.script_runners)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "scheduler.db"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    def allowed_extensions(self) -> set[str]:
        exts = set(DEFAULT_SCRIPT_EXTENSIONS)
        for ext in self.script_runners:
            cleaned = ext.strip().lower()
            if cleaned:
                if not cleaned.startswith("."):
                    cleaned = "." + cleaned
                exts.add(cleaned)
        return exts

    def is_supported_script(self, path: Path) -> bool:
        return path.suffix.lower() in self.allowed_extensions()

    def get_root(self, root_id: str) -> ScriptRoot | None:
        for root in self.scripts_roots:
            if root.id == root_id:
                return root
        return None

    def resolve_safe_path(self, root_id: str, rel_path: str = "") -> tuple[ScriptRoot, Path]:
        """Resolve a relative path strictly inside an allowed ScriptRoot."""
        root = self.get_root(root_id)
        if root is None:
            raise ValueError(f"Unknown folder root: {root_id}")

        raw_rel = rel_path or ""
        if "\x00" in raw_rel or any(ord(ch) < 32 for ch in raw_rel):
            raise ValueError("Invalid characters in path.")
        if "\\" in raw_rel or "//" in raw_rel or ":" in raw_rel or raw_rel.startswith("/"):
            raise ValueError("Invalid path format.")

        cleaned = raw_rel.rstrip("/")
        if cleaned:
            _validate_rel_parts(cleaned.split("/"))

        root_resolved = root.path.resolve()
        candidate = (root_resolved / cleaned).resolve() if cleaned else root_resolved

        try:
            resolved_rel = candidate.relative_to(root_resolved)
        except ValueError as exc:
            raise ValueError("Path escapes the allowed analytics folder.") from exc

        if resolved_rel.parts:
            _validate_rel_parts(resolved_rel.parts)

        return root, candidate

    def resolve_script_reference(self, script_ref: str) -> tuple[ScriptRoot, Path, str]:
        """Resolve a stored script reference ('root_id:relative/path.py') safely."""
        if not isinstance(script_ref, str) or not script_ref.strip():
            raise ValueError("Script reference is required.")

        ref = script_ref.strip()
        if "\x00" in ref or any(ord(ch) < 32 for ch in ref):
            raise ValueError("Invalid characters in script reference.")
        if ref.startswith(("\\\\", "//")):
            raise ValueError("UNC paths are not allowed.")

        if ":" in ref:
            prefix, remainder = ref.split(":", 1)
            if self.get_root(prefix) is not None:
                root, resolved = self.resolve_safe_path(prefix, remainder)
                rel_clean = resolved.relative_to(root.path.resolve()).as_posix()
                return root, resolved, rel_clean

            # Allow legacy Windows drive-letter direct path ONLY if it has no ADS colon
            # and resolves inside a configured root.
            is_win_drive = len(prefix) == 1 and prefix.isalpha() and remainder.startswith(("\\", "/"))
            if not is_win_drive:
                raise ValueError(f"Unknown folder root: {prefix}")
            if ":" in remainder:
                raise ValueError("Alternate data streams are not allowed.")

        # Legacy direct path fallback: must be an absolute path inside a configured root
        candidate_raw = Path(ref)
        if not candidate_raw.is_absolute():
            raise ValueError("Script reference must use 'root_id:relative/path' format.")

        raw_drive = candidate_raw.drive.lower()
        for root in self.scripts_roots:
            root_resolved = root.path.resolve()
            if raw_drive and root_resolved.drive.lower() != raw_drive:
                continue
            try:
                resolved_direct = candidate_raw.resolve()
                rel_direct = resolved_direct.relative_to(root_resolved)
            except (OSError, ValueError):
                continue
            rel_posix = rel_direct.as_posix()
            root_checked, resolved_checked = self.resolve_safe_path(root.id, rel_posix)
            return root_checked, resolved_checked, resolved_checked.relative_to(root_resolved).as_posix()

        raise ValueError("Script path must live inside a configured analytics folder.")


def load_config(config_path: Path | str | None = None) -> AppConfig:
    if config_path is None:
        env_path = os.environ.get("SCHEDULER_CONFIG")
        cfg_file = Path(env_path) if env_path else Path(__file__).resolve().parent.parent / "config.json"
    else:
        cfg_file = Path(config_path)

    base_dir = cfg_file.resolve().parent
    raw: dict[str, Any] = {}
    if cfg_file.exists():
        raw = json.loads(cfg_file.read_text(encoding="utf-8"))

    def resolve_dir(val: str) -> Path:
        p = Path(val)
        return p if p.is_absolute() else (base_dir / p).resolve()

    roots: list[ScriptRoot] = []
    seen_root_ids: set[str] = set()
    for idx, item in enumerate(raw.get("scripts_roots") or []):
        root_id = str(item.get("id") or f"root_{idx}").strip()
        if root_id in seen_root_ids:
            logger.warning("Duplicate scripts_roots id '%s' in %s; keeping first.", root_id, cfg_file)
            continue
        seen_root_ids.add(root_id)
        label = str(item.get("label") or root_id).strip()
        path = resolve_dir(str(item.get("path") or "."))
        roots.append(ScriptRoot(id=root_id, label=label, path=path))

    if not roots:
        roots.append(ScriptRoot(id="projects", label="Analytics Projects", path=(base_dir / "sample_scripts").resolve()))

    data_dir = resolve_dir(str(raw.get("data_dir") or "./data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "logs").mkdir(parents=True, exist_ok=True)

    script_runners = _normalize_script_runners(raw.get("script_runners") or {})

    raw_python_exe = str(raw.get("python_exe") or "").strip()
    raw_powershell_exe = str(raw.get("powershell_exe") or "").strip()
    raw_port = raw.get("port")
    port = int(raw_port) if raw_port is not None and raw_port != "" else 8787

    return AppConfig(
        base_dir=base_dir,
        app_name=str(raw.get("app_name") or "Analytics Scheduler"),
        host=str(raw.get("host") or "127.0.0.1"),
        port=port,
        dev_mode=bool(raw.get("dev_mode", False)),
        dev_user=str(raw.get("dev_user") or "Analyst"),
        iis_user_header=str(raw.get("iis_user_header") or "X-IIS-User"),
        python_exe=raw_python_exe or _default_python_exe(),
        powershell_exe=raw_powershell_exe or _default_powershell_exe(),
        script_runners=script_runners,
        max_concurrent_runs=_clamp_int(raw, "max_concurrent_runs", 4, 1, 16),
        default_timeout_minutes=_clamp_int(raw, "default_timeout_minutes", 45, 1, 480),
        log_retention_days=_clamp_int(raw, "log_retention_days", 30, 1, 365),
        misfire_grace_minutes=_clamp_int(raw, "misfire_grace_minutes", 60, 0, 1440),
        max_log_mb=_clamp_int(raw, "max_log_mb", 25, 1, 500),
        data_dir=data_dir,
        scripts_roots=roots,
    )


def browse_directory(config: AppConfig, root_id: str, rel_path: str = "") -> dict[str, Any]:
    """List folders and supported scripts safely within an allowed root."""
    root, target = config.resolve_safe_path(root_id, rel_path)
    root_resolved = root.path.resolve()

    if not target.exists() or not target.is_dir():
        raise FileNotFoundError("Folder not found.")

    folders: list[dict[str, Any]] = []
    scripts: list[dict[str, Any]] = []

    try:
        entries = list(target.iterdir())
    except PermissionError as exc:
        raise PermissionError("Access denied to this folder.") from exc
    except OSError as exc:
        raise FileNotFoundError("Folder could not be read.") from exc

    for entry in entries:
        name = entry.name
        if name.startswith(".") or name.lower() in EXCLUDED_DIRS:
            continue

        # Always resolve and verify containment for ALL entries (blocks symlinks and NTFS junctions)
        try:
            resolved_entry = entry.resolve()
            resolved_rel = resolved_entry.relative_to(root_resolved)
        except (OSError, ValueError):
            continue

        if any(part.startswith(".") or part.lower() in EXCLUDED_DIRS for part in resolved_rel.parts):
            continue

        try:
            rel = entry.relative_to(root_resolved).as_posix()
            is_dir = entry.is_dir()
            if is_dir:
                has_readme = _safe_is_file_within(entry / "README.md", root_resolved)
                has_script_dir = any(_safe_is_dir(entry / sub) for sub in PROJECT_SUBDIRS)
                folders.append(
                    {
                        "name": name,
                        "path": rel,
                        "is_project": has_readme or has_script_dir,
                    }
                )
            elif entry.is_file() and config.is_supported_script(entry):
                stat = entry.stat()
                scripts.append(
                    {
                        "name": name,
                        "path": rel,
                        "ext": entry.suffix.lower(),
                        "script_ref": f"{root.id}:{rel}",
                        "size_bytes": stat.st_size,
                        "modified_at": stat.st_mtime,
                    }
                )
        except OSError:
            continue

    folders.sort(key=lambda item: item["name"].lower())
    scripts.sort(key=lambda item: item["name"].lower())

    rel_current = "" if target == root_resolved else target.relative_to(root_resolved).as_posix()
    breadcrumbs: list[dict[str, str]] = [{"label": root.label, "path": ""}]
    if rel_current:
        parts = rel_current.split("/")
        for idx, part in enumerate(parts):
            breadcrumbs.append({"label": part, "path": "/".join(parts[: idx + 1])})

    return {
        "root_id": root.id,
        "root_label": root.label,
        "path": rel_current,
        "breadcrumbs": breadcrumbs,
        "folders": folders,
        "scripts": scripts,
    }


def _humanize_name(raw_name: str) -> str:
    stem = Path(raw_name).stem
    cleaned = re.sub(r"[_\-]+", " ", stem).strip()
    return " ".join(w.capitalize() if not w.isupper() else w for w in cleaned.split()) or "Untitled Script"


def _parse_readme(readme_path: Path) -> tuple[str, str]:
    """Extract friendly title and one-line summary from a project README.md."""
    try:
        text = readme_path.read_text(encoding="utf-8", errors="replace")[:8192]
    except OSError:
        return "", ""

    # Strip YAML frontmatter if present
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            text = parts[2]

    # Strip HTML comments
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)

    title = ""
    summary_lines: list[str] = []
    in_summary = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if in_summary and summary_lines:
                break
            continue
        if line.startswith("#"):
            if not title:
                title = line.lstrip("#").strip()
            elif summary_lines:
                break
            continue
        if line.startswith(("|", "```", ">", "---", "- [", "* [")):
            continue
        # Plain text paragraph
        in_summary = True
        summary_lines.append(line)
        if len(" ".join(summary_lines)) >= 220:
            break

    summary = " ".join(summary_lines).strip()
    # Strip basic markdown emphasis/links for clean card display
    summary = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", summary)
    summary = re.sub(r"[*_`]", "", summary)
    if len(summary) > 220:
        summary = summary[:217].rstrip() + "..."
    return title, summary


def inspect_script(config: AppConfig, script_ref: str) -> dict[str, Any]:
    """Inspect a supported script and detect nearby project README.md & reports/ folder."""
    root, script_path, rel_path = config.resolve_script_reference(script_ref)
    if not script_path.is_file() or not config.is_supported_script(script_path):
        raise FileNotFoundError("Supported script not found.")

    root_resolved = root.path.resolve()
    script_dir = script_path.parent
    project_dir = script_dir

    # Standard analytics project folder: <project>/<python|scripts|powershell|src>/script.*
    if script_dir.name.lower() in PROJECT_SUBDIRS and script_dir.parent != root_resolved:
        project_dir = script_dir.parent

    readme_title = ""
    readme_summary = ""
    readme_found = False

    for candidate_dir in (project_dir, script_dir):
        try:
            candidate_dir.resolve().relative_to(root_resolved)
        except (OSError, ValueError):
            continue
        readme = candidate_dir / "README.md"
        try:
            readme_resolved = readme.resolve()
            readme_resolved.relative_to(root_resolved)
        except (OSError, ValueError):
            continue
        if readme_resolved.is_file():
            readme_title, readme_summary = _parse_readme(readme_resolved)
            readme_found = True
            break

    suggested_name = readme_title or (
        _humanize_name(project_dir.name)
        if project_dir != root_resolved and project_dir != script_dir
        else _humanize_name(script_path.name)
    )

    suggested_freshness = ""
    reports_dir = project_dir / "reports"
    try:
        reports_resolved = reports_dir.resolve()
        reports_resolved.relative_to(root_resolved)
        if reports_resolved.is_dir():
            rel_reports = reports_dir.relative_to(root_resolved).as_posix()
            suggested_freshness = f"{root.id}:{rel_reports}/freshness.json"
    except (OSError, ValueError):
        pass

    return {
        "script_ref": f"{root.id}:{rel_path}",
        "script_name": script_path.name,
        "suggested_name": suggested_name,
        "suggested_description": readme_summary,
        "readme_found": readme_found,
        "suggested_freshness_path": suggested_freshness,
    }
