"""`n8n-cli setup` — install hooks into Claude Code.

Drops a skill, a slash-command, and (optionally) a CLAUDE.md snippet into
the user's ~/.claude/ tree so Claude Code picks up n8n-cli automatically.

Everything is idempotent: re-running `install` refreshes files in place.
`uninstall` removes only what we dropped — never edits unrelated content.
"""

from __future__ import annotations

import os
import stat
from importlib.resources import files
from pathlib import Path
from typing import Annotated

import typer

from n8n_cli import __version__
from n8n_cli.output.jsonout import emit

app = typer.Typer(
    help="Install n8n-cli hooks into Claude Code (skill + slash command).",
    no_args_is_help=True,
)

_CLAUDE_HOME_ENV = "CLAUDE_HOME"
_DEFAULT_CLAUDE_HOME = Path.home() / ".claude"
_SKILL_DIR_NAME = "n8n-cli"
_SLASH_NAME = "n8n.md"
_MARKER_BEGIN = "<!-- n8n-cli:begin -->"
_MARKER_END = "<!-- n8n-cli:end -->"


def _claude_home() -> Path:
    override = os.environ.get(_CLAUDE_HOME_ENV)
    return Path(override).expanduser() if override else _DEFAULT_CLAUDE_HOME


def _resource_text(name: str) -> str:
    return (files("n8n_cli.resources") / name).read_text(encoding="utf-8")


def _write(path: Path, body: str, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(stat.S_IMODE(mode))


def _config_dirs_ready() -> Path:
    """Create ~/.config/n8n-cli/{sessions,} with sane perms. Returns the root."""
    from platformdirs import user_config_dir

    root = Path(user_config_dir("n8n-cli"))
    (root / "sessions").mkdir(parents=True, exist_ok=True)
    (root / "sessions").chmod(0o700)
    return root


def _upsert_claude_md(claude_md: Path, snippet: str) -> str:
    """Insert/replace the snippet between markers. Returns 'added'/'updated'/'unchanged'."""
    snippet = snippet.strip() + "\n"
    if not claude_md.exists():
        claude_md.write_text(snippet + "\n", encoding="utf-8")
        return "added"

    existing = claude_md.read_text(encoding="utf-8")
    if _MARKER_BEGIN in existing and _MARKER_END in existing:
        before, _, rest = existing.partition(_MARKER_BEGIN)
        _, _, after = rest.partition(_MARKER_END)
        new_content = before + snippet.rstrip() + "\n" + after.lstrip("\n")
        if new_content == existing:
            return "unchanged"
        claude_md.write_text(new_content, encoding="utf-8")
        return "updated"

    sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    claude_md.write_text(existing + sep + snippet, encoding="utf-8")
    return "added"


def _remove_claude_md_block(claude_md: Path) -> bool:
    if not claude_md.exists():
        return False
    existing = claude_md.read_text(encoding="utf-8")
    if _MARKER_BEGIN not in existing or _MARKER_END not in existing:
        return False
    before, _, rest = existing.partition(_MARKER_BEGIN)
    _, _, after = rest.partition(_MARKER_END)
    new_content = (before.rstrip() + "\n" + after.lstrip("\n")).strip() + "\n"
    claude_md.write_text(new_content, encoding="utf-8")
    return True


@app.command("install")
def install(
    with_claude_md: Annotated[
        bool,
        typer.Option(
            "--with-claude-md",
            help="Also append a hint block to ~/.claude/CLAUDE.md (off by default — "
            "skills already activate on demand).",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite existing skill/command files without prompting."),
    ] = False,
) -> None:
    """Install the skill, slash-command, and config scaffolding."""
    home = _claude_home()
    skill_path = home / "skills" / _SKILL_DIR_NAME / "SKILL.md"
    slash_path = home / "commands" / _SLASH_NAME

    installed: list[str] = []

    for dest, resource in (
        (skill_path, "SKILL.md"),
        (slash_path, "slash-n8n.md"),
    ):
        body = _resource_text(resource)
        if dest.exists() and not force and dest.read_text(encoding="utf-8") == body:
            installed.append(f"unchanged: {dest}")
            continue
        _write(dest, body)
        installed.append(f"wrote: {dest}")

    cfg_root = _config_dirs_ready()
    installed.append(f"config-dir: {cfg_root}")

    claude_md_status: str | None = None
    if with_claude_md:
        snippet = _resource_text("claude-md-snippet.md")
        claude_md_status = _upsert_claude_md(home / "CLAUDE.md", snippet)

    emit(
        {
            "ok": True,
            "version": __version__,
            "claude_home": str(home),
            "steps": installed,
            "claude_md": claude_md_status,
            "next": [
                "n8n-cli instance add <name> --url https://... --api-key <JWT> --use",
                "n8n-cli auth login --email <you@x> --password-stdin",
                "n8n-cli auth status",
            ],
        }
    )


@app.command("uninstall")
def uninstall() -> None:
    """Remove the skill, slash-command, and the CLAUDE.md snippet block."""
    home = _claude_home()
    removed: list[str] = []

    skill_dir = home / "skills" / _SKILL_DIR_NAME
    if skill_dir.exists():
        for child in sorted(skill_dir.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink()
            elif child.is_dir():
                child.rmdir()
        skill_dir.rmdir()
        removed.append(f"removed: {skill_dir}")

    slash_path = home / "commands" / _SLASH_NAME
    if slash_path.exists():
        slash_path.unlink()
        removed.append(f"removed: {slash_path}")

    if _remove_claude_md_block(home / "CLAUDE.md"):
        removed.append(f"cleared-block-in: {home / 'CLAUDE.md'}")

    emit({"ok": True, "removed": removed})


@app.command("status")
def status() -> None:
    """Report what's installed and whether the CLI is ready to use."""
    home = _claude_home()
    from platformdirs import user_config_dir

    cfg = Path(user_config_dir("n8n-cli"))
    skill_path = home / "skills" / _SKILL_DIR_NAME / "SKILL.md"
    slash_path = home / "commands" / _SLASH_NAME
    claude_md = home / "CLAUDE.md"
    claude_md_block = claude_md.exists() and _MARKER_BEGIN in claude_md.read_text(encoding="utf-8")

    emit(
        {
            "version": __version__,
            "claude_home": str(home),
            "config_dir": str(cfg),
            "installed": {
                "skill": skill_path.exists(),
                "slash_command": slash_path.exists(),
                "claude_md_block": claude_md_block,
                "config_dir": cfg.exists(),
            },
        }
    )
