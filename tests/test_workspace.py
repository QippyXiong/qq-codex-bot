from qq_codex_bot.workspace import ensure_workspace


def test_existing_workspace_guidance_is_preserved(tmp_path):
    instructions = tmp_path / "AGENTS.md"
    original = b"# My project\r\n\r\nKeep my custom instructions.\r\n"
    instructions.write_bytes(original)
    before = instructions.stat().st_mtime_ns

    ensure_workspace(tmp_path)
    ensure_workspace(tmp_path)

    assert instructions.read_bytes() == original
    assert instructions.stat().st_mtime_ns == before


def test_generated_workspace_guidance_can_be_customized_without_being_replaced(tmp_path):
    workspace = tmp_path / "custom-project"
    ensure_workspace(workspace)
    instructions = workspace / "AGENTS.md"
    custom = instructions.read_text() + "\n## 项目要求\n使用 Python。\n"
    instructions.write_text(custom)

    ensure_workspace(workspace)

    assert instructions.read_text() == custom
