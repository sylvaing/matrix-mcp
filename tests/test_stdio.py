"""End-to-end smoke test of the stdio transport: boot the real entry point in
a subprocess, run the MCP initialize handshake, list tools."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "0"},
    },
}


@pytest.mark.timeout(30)
def test_stdio_boots_and_lists_tools():
    env = dict(
        os.environ,
        TENANT_MAIN_BASE_URL="https://matrix.example.org",
        TENANT_MAIN_USER_ID="@alice:example.org",
        TENANT_MAIN_TOKEN="unused-in-this-test",
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "matrix_mcp", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        text=True,
    )
    try:
        def send(obj):
            proc.stdin.write(json.dumps(obj) + "\n")
            proc.stdin.flush()

        send(INIT)
        init_resp = json.loads(proc.stdout.readline())
        assert init_resp["id"] == 1
        assert "serverInfo" in init_resp["result"]

        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools_resp = json.loads(proc.stdout.readline())
        names = {t["name"] for t in tools_resp["result"]["tools"]}
        assert len(names) == 13
        assert "matrix_send_message" in names
        assert {"matrix_list_direct_messages", "matrix_get_unread"} <= names
    finally:
        proc.terminate()
        proc.wait(timeout=10)
