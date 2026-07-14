"""Dogfood the real stdio MCP transport with separate server processes."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import subprocess
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest


async def _connect(stack: AsyncExitStack, workspace: Path, home: Path) -> ClientSession:
    env = dict(os.environ)
    env["XTALK_HOME"] = str(home)
    read, write = await stack.enter_async_context(stdio_client(StdioServerParameters(
        command=sys.executable, args=["-m", "xtalk.server"], cwd=workspace, env=env,
    )))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return session


async def _call(session: ClientSession, name: str, arguments: dict) -> dict:
    result = await session.call_tool(name, arguments)
    assert not result.isError
    return json.loads(result.content[0].text)


@pytest.mark.skipif(os.name == "nt", reason="MCP SDK stdio_client cleanup hangs on Windows; direct stdio smoke is covered separately")
def test_two_real_mcp_processes_and_restart(tmp_path: Path) -> None:
    asyncio.run(_dogfood(tmp_path))


async def _dogfood(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    home = tmp_path / "home"
    workspace.mkdir()

    async with AsyncExitStack() as stack:
        a = await _connect(stack, workspace, home)
        b = await _connect(stack, workspace, home)
        reg_a = await _call(a, "xtalk_register", {"alias": "coder", "client": "codex", "capabilities": ["long_poll"]})
        reg_b = await _call(b, "xtalk_register", {"alias": "reviewer", "client": "codex", "capabilities": ["long_poll"]})
        assert reg_a["room_id"] == reg_b["room_id"]
        assert reg_a["room_restored"] is False
        assert reg_b["room_restored"] is True

        ask = await _call(a, "xtalk_ask", {"to": "reviewer", "body": "dogfood review"})
        read = await _call(b, "xtalk_read", {"thread": ask["thread_id"]})
        assert read["messages"][0]["body"] == "dogfood review"
        await _call(b, "xtalk_reply", {
            "thread": ask["thread_id"], "body": "dogfood ok", "in_reply_to": ask["msg_id"],
        })
        waited = await _call(a, "xtalk_wait", {"in_reply_to": ask["msg_id"], "timeout_ms": 2000})
        assert waited["event"]["kind"] == "reply"

    # A brand-new MCP process discovers the durable project room after restart.
    async with AsyncExitStack() as restart_stack:
        c = await _connect(restart_stack, workspace, home)
        reg_c = await _call(c, "xtalk_register", {"alias": "coder-next", "client": "codex", "capabilities": ["long_poll"]})
        assert reg_c["room_id"] == reg_a["room_id"]
        assert reg_c["project_id"] == reg_a["project_id"]
        assert reg_c["room_restored"] is True
        assert (workspace / ".xtalk" / "project.json").exists()


def test_server_stdio_initialize_without_sdk_client(tmp_path: Path) -> None:
    """Validate the actual stdio wire path, including on Windows.

    The SDK's client context-manager cleanup currently hangs on Windows, so
    this smoke test speaks one JSON-RPC request directly and closes stdin.
    """
    workspace = tmp_path / "project"
    workspace.mkdir()
    env = dict(os.environ)
    env["XTALK_HOME"] = str(tmp_path / "home")
    request = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "xtalk-smoke", "version": "1"},
        },
    }) + "\n"
    process = subprocess.Popen(
        [sys.executable, "-m", "xtalk.server"], cwd=workspace, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        stdout, stderr = process.communicate(request, timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        pytest.fail(f"xtalk stdio initialize timed out: {stderr}")
    assert process.returncode == 0, stderr
    response = json.loads(stdout.splitlines()[0])
    assert response["id"] == 1
    assert response["result"]["serverInfo"]["name"] == "xtalk"
