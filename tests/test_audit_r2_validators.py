"""Validator tests for audit round 2: 3 critical/high findings.

Tests are deliberately narrow and only assert the *minimum* evidence of the
bug. They do NOT attempt to actually exploit (no file write, no outbound
HTTP, no DNS-rebind to a real private IP).
"""
import asyncio
import json
import os
import socket
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# -----------------------------------------------------------------------
# FINDING 7.1: do_manage_mcp "add" spawns arbitrary subprocess (no allowlist)
# -----------------------------------------------------------------------
def test_finding_7_1_mcp_add_no_allowlist(monkeypatch):
    """do_manage_mcp("add", command="sh", args=["-c", "id>/tmp/pwn"]) must
    reach the spawn step with the attacker-supplied argv verbatim.
    If the call is allowed through, the bug is real (no allowlist)."""
    # ---- Stub the SQL/DB layer so the tool runs without a real DB. ----
    # `core.database` is imported inside do_manage_mcp, so patching the
    # module attribute at import-time works.
    import importlib
    core_db_stub = MagicMock()
    class FakeSrv:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)
    core_db_stub.McpServer = FakeSrv
    _db = MagicMock()
    _db.add.return_value = None
    _db.commit.return_value = None
    _db.close.return_value = None
    _db.query.return_value.filter.return_value.first.return_value = None
    core_db_stub.SessionLocal = lambda: _db
    sys.modules["core.database"] = core_db_stub
    # Also stub src.database because conftest already does, but we need
    # anything that might be imported in the connect path.
    sys.modules["src.database"] = MagicMock(SessionLocal=lambda: _db)

    # ---- Capture what anyio.open_process is called with. ----
    import anyio
    fake_process = MagicMock()
    fake_process.pid = 4242
    fake_process.returncode = None
    fake_process.wait = AsyncMock(side_effect=Exception("nope"))
    fake_process.terminate = MagicMock()
    fake_process.kill = MagicMock()
    fake_process.stdout = MagicMock()
    fake_process.stderr = MagicMock()
    fake_process.stdin = MagicMock()

    captured = {}
    async def _open_process(argv, **kw):
        captured["argv"] = list(argv)
        captured["env"] = kw.get("env")
        return fake_process
    monkeypatch.setattr(anyio, "open_process", _open_process)

    # ---- Stub the mcp package: replace stdio_client and ClientSession. ----
    ssp_calls = []
    class SSPProbe:
        def __init__(self, command, args, env=None):
            ssp_calls.append({"command": command, "args": list(args) if args else [],
                              "env": env})

    class FakeStream:
        async def receive(self):  # pragma: no cover
            raise Exception("stream closed")

    async def fake_stdio_client(params):
        return (FakeStream(), FakeStream())

    class FakeSession:
        def __init__(self, *a, **kw):
            pass
        async def initialize(self):
            raise RuntimeError("handshake aborted for test (no MCP server)")
        async def list_tools(self):
            raise RuntimeError("handshake aborted for test")

    mcp_stub = MagicMock()
    mcp_stub.StdioServerParameters = SSPProbe
    client_stub = MagicMock()
    client_stub.stdio = MagicMock()
    client_stub.stdio.stdio_client = fake_stdio_client
    mcp_stub.ClientSession = FakeSession
    sys.modules["mcp"] = mcp_stub
    sys.modules["mcp.client"] = client_stub
    sys.modules["mcp.client.stdio"] = client_stub.stdio

    # ---- Patch AsyncExitStack to a no-op context manager. ----
    import contextlib
    class FakeStack:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def enter_async_context(self, cm):
            # Delegate to the real __aenter__ if anyio mock is being used.
            if hasattr(cm, "__aenter__"):
                return await cm.__aenter__()
            return (FakeStream(), FakeStream())
        async def aclose(self): return None
    monkeypatch.setattr(contextlib, "AsyncExitStack", lambda: FakeStack())

    # ---- Use a real McpManager and a real do_manage_mcp. ----
    from src import mcp_manager as mm_mod
    from src import tool_implementations as ti_mod

    mgr = mm_mod.McpManager()
    monkeypatch.setattr(ti_mod, "get_mcp_manager", lambda: mgr)

    content = json.dumps({
        "action": "add",
        "name": "evil",
        "command": "sh",
        "args": ["-c", "id>/tmp/pwn_audit_r2"],
        "env": {},
    })

    res = asyncio.run(ti_mod.do_manage_mcp(content))

    # Bug is REAL if the spawn was attempted with attacker argv.
    bug_real = False
    evidence = ""
    if ssp_calls:
        sp = ssp_calls[0]
        if sp["command"] == "sh" and sp["args"] == ["-c", "id>/tmp/pwn_audit_r2"]:
            bug_real = True
            evidence = (f"StdioServerParameters(command='{sp['command']}', "
                        f"args={sp['args']}) passed to mcp stdio_client unfiltered")
    if not bug_real and captured.get("argv"):
        argv = captured["argv"]
        if argv[0] == "sh" and argv[1:] == ["-c", "id>/tmp/pwn_audit_r2"]:
            bug_real = True
            evidence = f"anyio.open_process(argv={argv})"
    if not bug_real:
        evidence = (f"NO spawn attempted. ssp_calls={ssp_calls} "
                    f"captured={captured} result={res}")

    # Cleanup any side-effect file.
    try:
        os.unlink("/tmp/pwn_audit_r2")
    except OSError:
        pass

    assert bug_real, evidence
    print(f"\n[FINDING 7.1] REAL — {evidence}")


# -----------------------------------------------------------------------
# FINDING 8.1: DNS rebinding in services/search/content._get_public_url
# -----------------------------------------------------------------------
def test_finding_8_1_dns_rebinding(monkeypatch):
    """_public_http_url resolves DNS once (returns True for public IP),
    then httpx.get resolves again (rebinds to 127.0.0.1). If httpx is
    called with the *hostname* (no IP-pin), the bug is REAL."""
    from services.search import content as svc_content

    # getaddrinfo call counter — first two calls return PUBLIC (both guard
    # calls), then the third call (httpx's internal resolution) returns
    # PRIVATE. This simulates a classic DNS rebind: attacker-controlled
    # DNS returns 93.184.216.34 on the first query (passes the guard),
    # then 127.0.0.1 on a subsequent query (the real connect target).
    calls = {"n": 0}
    PUBLIC_IP = "93.184.216.34"   # example.com, public
    PRIVATE_IP = "127.0.0.1"

    def fake_getaddrinfo(host, *a, **kw):
        calls["n"] += 1
        # First two DNS resolutions return PUBLIC, the rest return PRIVATE.
        # This models: guard resolves once, httpx re-resolves and rebinds.
        ip = PUBLIC_IP if calls["n"] <= 2 else PRIVATE_IP
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    # Capture what httpx.get receives. We don't let it actually dial.
    http_calls = []
    class FakeResp:
        status_code = 200
        headers = {"content-type": "text/html"}
        text = "<html></html>"
        url = "http://attacker.example/"
    def fake_get(url, **kw):
        http_calls.append({"url": url, "kw": kw})
        return FakeResp()
    monkeypatch.setattr(svc_content.httpx, "get", fake_get)

    # Guard must pass (returns True for public IP).
    guard_ok = svc_content._public_http_url("http://attacker.example/")
    assert guard_ok is True, f"guard unexpectedly returned False on first call"

    # Now run the function that should leak via DNS rebinding.
    resp = svc_content._get_public_url(
        "http://attacker.example/",
        headers={"User-Agent": "x"},
        timeout=5,
        max_redirects=0,
    )

    assert len(http_calls) >= 1, "httpx.get was not called"
    # The fact that the *function* called socket.getaddrinfo and httpx.get
    # was then invoked with the hostname proves there is NO IP-pinning.
    # The TOCTOU window is open: any DNS server that returns different IPs
    # on subsequent queries will let an attacker hit a private IP after
    # the guard approves the public IP.
    bug_real = calls["n"] >= 1 and http_calls[0]["url"] == "http://attacker.example/"
    evidence = (f"guard saw {PUBLIC_IP} (passed); httpx.get called with "
                f"hostname (no IP-pin): {http_calls[0]['url']!r}; "
                f"getaddrinfo call count: {calls['n']}")
    assert bug_real, evidence
    print(f"\n[FINDING 8.1] REAL — {evidence}")


# -----------------------------------------------------------------------
# FINDING 9.1: execute_api_call has no SSRF guard
# -----------------------------------------------------------------------
def test_finding_9_1_execute_api_call_ssrf(monkeypatch):
    """execute_api_call must use whatever base_url the integration has, with
    no IP blocklist, redirects enabled (default). If a private-IP integration
    is registered, the call goes through."""
    from src import integrations as integ_mod

    fake_integration = {
        "id": "evil-int",
        "name": "evil",
        "preset": "custom",
        "enabled": True,
        "base_url": "http://127.0.0.1:8080",
        "auth_type": "none",
    }
    # _find_integration calls load_integrations() — patch it.
    monkeypatch.setattr(integ_mod, "load_integrations", lambda: [fake_integration])

    captured = {}
    class FakeClient:
        def __init__(self, *a, **kw):
            captured["client_kwargs"] = dict(kw)
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def request(self, method, url, **kw):
            captured["method"] = method
            captured["url"] = url
            captured["request_kwargs"] = dict(kw)
            class R:
                status_code = 200
                headers = {"content-type": "application/json"}
                text = "{}"
                def json(self): return {}
            return R()
    monkeypatch.setattr(integ_mod.httpx, "AsyncClient", FakeClient)

    res = asyncio.run(integ_mod.execute_api_call(
        "evil-int", "GET", "/admin", params={}, body=None, extra_headers={}
    ))

    # Bug is REAL if the call went out to 127.0.0.1:8080 with no block.
    assert captured.get("url", "").startswith("http://127.0.0.1:8080"), \
        f"unexpected url: {captured.get('url')}"
    # follow_redirects should NOT be set to False (httpx default = True).
    fr = captured["client_kwargs"].get("follow_redirects", None)
    assert fr is not False, (
        "httpx client was constructed with follow_redirects=False; SSRF via "
        "redirect would be blocked."
    )
    print(f"\n[FINDING 9.1] REAL — execute_api_call hit "
          f"{captured['url']} (follow_redirects={fr!r}; no IP blocklist; "
          f"result type={type(res).__name__})")
