"""Installer paths and schemas used by real client CLIs."""
from __future__ import annotations

import json

from xtalk import cli


def test_claude_installer_writes_user_mcp_schema(tmp_path, monkeypatch):
    config = tmp_path / ".claude.json"
    monkeypatch.setitem(cli.CLIENT_CONFIGS, "claude-code", config)

    result = cli._install_claude_code("/bin/xtalk-mcp", dry_run=False)

    entry = json.loads(config.read_text())["mcpServers"]["xtalk"]
    assert "wrote" in result
    assert entry == {"type": "stdio", "command": "/bin/xtalk-mcp", "args": [], "env": {}}


def test_antigravity_installer_targets_cli_config(tmp_path, monkeypatch):
    config = tmp_path / ".gemini" / "antigravity-cli" / "mcp_config.json"
    monkeypatch.setitem(cli.CLIENT_CONFIGS, "antigravity", config)

    result = cli._install_json_client("antigravity", config, "/bin/xtalk-mcp", dry_run=False)

    assert "wrote" in result
    assert json.loads(config.read_text())["mcpServers"]["xtalk"]["command"] == "/bin/xtalk-mcp"
