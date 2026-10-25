"""Initialize persistent guidance without replacing a user's workspace instructions."""

from importlib.resources import files
from pathlib import Path


def ensure_workspace(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    instructions = workspace / "AGENTS.md"
    if instructions.exists():
        return
    template = files("qq_codex_bot").joinpath("templates/AGENTS.md").read_text(encoding="utf-8")
    try:
        # Exclusive creation also preserves a file/symlink created since the existence check.
        with instructions.open("x", encoding="utf-8") as output:
            output.write(template)
    except FileExistsError:
        pass
