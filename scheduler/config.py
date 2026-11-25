"""Configuration loading, safe path boundaries, and project folder inspection."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    dev_mode: bool = True
    dev_user: str = "Analyst"
    iis_user_header: str = "X-IIS-User"
    python_exe: str = "python"
    max_concurrent_runs: int = 4
    default_timeout_minutes: int = 45
    log_retention_days: int = 30
    data_dir: Path = field(default_factory=lambda: Path("./data"))
    scripts_roots: list[ScriptRoot] = field(default_factory=list)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "scheduler.db"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

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

        cleaned = (rel_path or "").replace("\\", "/").strip("/")
        if ".." in cleaned.split("/"):
            raise ValueError("Path traversal is not allowed.")

        root_resolved = root.path.resolve()
        candidate = (root_resolved / cleaned).resolve() if cleaned else root_resolved

        try:
            candidate.relative_to(root_resolved)
        except ValueError as exc:
            raise ValueError("Path escapes the allowed analytics folder.") from exc

        return root, candidate

    def resolve_script_reference(self, script_ref: str) -> tuple[ScriptRoot, Path, str]:
        """Resolve a stored script reference ('root_id:relative/path.py') safely."""
        if ":" not in script_ref:
            # Support legacy or direct path if it falls inside an allowed root
            raw = Path(script_ref).resolve()
            for root in self.scripts_roots:
                root_resolved = root.path.resolve()
                try:
                    rel = raw.relative_to(root_resolved)
                    return root, raw, rel.as_posix()
                except ValueError:
                    continue
            raise ValueError("Script path must live inside a configured analytics folder.")

        root_id, rel_path = script_ref.split(":", 1)
        root, resolved = self.resolve_safe_path(root_id, rel_path)
        rel_clean = resolved.relative_to(root.path.resolve()).as_posix()
        return root, resolved, rel_clean


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
    for idx, item in enumerate(raw.get("scripts_roots") or []):
        root_id = str(item.get("id") or f"root_{idx}").strip()
        label = str(item.get("label") or root_id).strip()
        path = resolve_dir(str(item.get("path") or "."))
        roots.append(ScriptRoot(id=root_id, label=label, path=path))

    if not roots:
        roots.append(ScriptRoot(id="projects", label="Analytics Projects", path=(base_dir / "sample_scripts").resolve()))

    data_dir = resolve_dir(str(raw.get("data_dir") or "./data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "logs").mkdir(parents=True, exist_ok=True)

    return AppConfig(
        base_dir=base_dir,
        app_name=str(raw.get("app_name") or "Analytics Scheduler"),
        host=str(raw.get("host") or "127.0.0.1"),
        port=int(raw.get("port") or 8787),
        dev_mode=bool(raw.get("dev_mode", False)),
        dev_user=str(raw.get("dev_user") or "Analyst"),
        iis_user_header=str(raw.get("iis_user_header") or "X-IIS-User"),
        python_exe=str(raw.get("python_exe") or "python"),
        max_concurrent_runs=max(1, min(16, int(raw.get("max_concurrent_runs") or 4))),
        default_timeout_minutes=max(1, min(480, int(raw.get("default_timeout_minutes") or 45))),
        log_retention_days=max(1, min(365, int(raw.get("log_retention_days") or 30))),
        data_dir=data_dir,
        scripts_roots=roots,
    )


def browse_directory(config: AppConfig, root_id: str, rel_path: str = "") -> dict[str, Any]:
    """List folders and .py scripts safely within an allowed root."""
    root, target = config.resolve_safe_path(root_id, rel_path)
    root_resolved = root.path.resolve()

    if not target.exists() or not target.is_dir():
        raise FileNotFoundError("Folder not found.")

    folders: list[dict[str, Any]] = []
    scripts: list[dict[str, Any]] = []

    try:
        entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError as exc:
        raise PermissionError("Access denied to this folder.") from exc

    for entry in entries:
        name = entry.name
        if name.startswith(".") or name in EXCLUDED_DIRS:
            continue
        if entry.is_symlink():
            # Never follow symlinks escaping root
            try:
                entry.resolve().relative_to(root_resolved)
            except ValueError:
                continue

        rel = entry.relative_to(root_resolved).as_posix()
        if entry.is_dir():
            has_readme = (entry / "README.md").is_file()
            has_python_dir = (entry / "python").is_dir()
            folders.append(
                {
                    "name": name,
                    "path": rel,
                    "is_project": has_readme or has_python_dir,
                }
            )
        elif entry.is_file() and entry.suffix.lower() == ".py":
            stat = entry.stat()
            scripts.append(
                {
                    "name": name,
                    "path": rel,
                    "script_ref": f"{root.id}:{rel}",
                    "size_bytes": stat.st_size,
                    "modified_at": stat.st_mtime,
                }
            )

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
    stem = re.sub(r"\.py$", "", raw_name, flags=re.IGNORECASE)
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
    """Inspect a .py script and detect nearby project README.md & reports/ folder."""
    root, script_path, rel_path = config.resolve_script_reference(script_ref)
    if not script_path.is_file() or script_path.suffix.lower() != ".py":
        raise FileNotFoundError("Python script not found.")

    root_resolved = root.path.resolve()
    script_dir = script_path.parent
    project_dir = script_dir

    # Standard analytics project folder: <project>/python/script.py
    if script_dir.name.lower() in {"python", "scripts", "src"} and script_dir.parent != root_resolved:
        project_dir = script_dir.parent

    readme_title = ""
    readme_summary = ""
    readme_found = False

    for candidate_dir in (project_dir, script_dir):
        try:
            candidate_dir.relative_to(root_resolved)
        except ValueError:
            continue
        readme = candidate_dir / "README.md"
        if readme.is_file():
            readme_title, readme_summary = _parse_readme(readme)
            readme_found = True
            break

    suggested_name = readme_title or (
        _humanize_name(project_dir.name)
        if project_dir != root_resolved and project_dir != script_dir
        else _humanize_name(script_path.name)
    )

    suggested_freshness = ""
    reports_dir = project_dir / "reports"
    if reports_dir.is_dir():
        try:
            rel_reports = reports_dir.relative_to(root_resolved).as_posix()
            suggested_freshness = f"{root.id}:{rel_reports}/freshness.json"
        except ValueError:
            pass

    return {
        "script_ref": f"{root.id}:{rel_path}",
        "script_name": script_path.name,
        "suggested_name": suggested_name,
        "suggested_description": readme_summary,
        "readme_found": readme_found,
        "suggested_freshness_path": suggested_freshness,
    }
