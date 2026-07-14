"""Installer paths and schemas used by real client CLIs."""
from __future__ import annotations

import json
from argparse import Namespace

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


def test_codex_installer_replaces_stale_command(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text('[model]\nname = "x"\n\n[mcp_servers.xtalk]\ncommand = "/old/xtalk-mcp"\n')
    monkeypatch.setitem(cli.CLIENT_CONFIGS, "codex", config)

    result = cli._install_codex("/new/xtalk-mcp", dry_run=False)

    assert "updated" in result
    text = config.read_text()
    assert 'command = "/new/xtalk-mcp"' in text
    assert "/old/xtalk-mcp" not in text
    assert '[model]\nname = "x"' in text


def test_invalid_json_config_makes_install_fail(tmp_path, monkeypatch):
    config = tmp_path / ".claude.json"
    config.write_text("{broken")
    monkeypatch.setitem(cli.CLIENT_CONFIGS, "claude-code", config)
    args = Namespace(server="/bin/xtalk-mcp", client=["claude-code"], dry_run=False)
    assert cli.cmd_install(args) == 1


def test_invalid_codex_toml_makes_install_fail(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text('[broken\nvalue = "x"')
    monkeypatch.setitem(cli.CLIENT_CONFIGS, "codex", config)
    args = Namespace(server="/bin/xtalk-mcp", client=["codex"], dry_run=False)
    assert cli.cmd_install(args) == 1


def test_uninstall_removes_only_xtalk_entry(tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {"xtalk": {"command": "x"}, "other": {"command": "y"}}}))

    result = cli._remove_client("cursor", config, dry_run=False)

    assert "removed" in result
    assert json.loads(config.read_text())["mcpServers"] == {"other": {"command": "y"}}
