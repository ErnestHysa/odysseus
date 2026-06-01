"""
Regression test for the manage_mcp RCE allowlist.

Background: do_manage_mcp 'add' previously accepted a user-controllable
command/args/env and passed them straight to mcp_manager.connect_server,
which spawned a subprocess with no allowlist. A prompt-injection payload
could register `command="sh", args=["-c", "id>/tmp/pwn"]` and get RCE.

This test asserts that the validator (_validate_mcp_command) refuses
the attack paths while allowing the legitimate built-in MCP entrypoints
(scripts inside mcp_servers/ run by an allowlisted interpreter).
"""
import sys
import pytest

sys.path.insert(0, "/Users/ernest/Desktop/DEVPROJECTS/odysseus")

from src.tool_implementations import _validate_mcp_command


class TestValidateMcpCommandRejectsAttacks:
    def test_shell_command_rejected(self):
        # The classic RCE: sh -c "id>/tmp/pwn"
        err = _validate_mcp_command("sh", ["-c", "id>/tmp/pwn"], {})
        assert err is not None
        assert "allowlist" in err.lower() or "not on" in err.lower()

    def test_bash_with_c_flag_rejected(self):
        err = _validate_mcp_command("bash", ["-c", "rm -rf $HOME"], {})
        assert err is not None
        assert "allowlist" in err.lower()

    def test_python3_with_c_flag_rejected(self):
        # The interpreter IS on the allowlist, but the -c flag is a shell escape
        err = _validate_mcp_command("python3", ["-c", "import os; os.system('id')"], {})
        assert err is not None
        assert "shell-escape" in err.lower() or "flag" in err.lower()

    def test_node_with_eval_rejected(self):
        err = _validate_mcp_command("node", ["--eval", "require('child_process').exec('id')"], {})
        assert err is not None

    def test_path_outside_project_rejected(self):
        # A path that's outside the project root should be rejected even
        # if the file exists.
        err = _validate_mcp_command("/tmp/evil.sh", [], {})
        assert err is not None
        assert "outside" in err.lower() or "project root" in err.lower()

    def test_usrlocalbin_path_rejected(self):
        err = _validate_mcp_command("/usr/local/bin/some-binary", [], {})
        assert err is not None
        assert "outside" in err.lower()

    def test_ld_preload_env_rejected(self):
        err = _validate_mcp_command("python3", ["mcp_servers/memory_server.py"],
                                     {"LD_PRELOAD": "/tmp/evil.so"})
        assert err is not None
        assert "forbidden" in err.lower() or "ld_preload" in err.lower()

    def test_path_env_rejected(self):
        err = _validate_mcp_command("python3", ["mcp_servers/memory_server.py"],
                                     {"PATH": "/tmp:$PATH"})
        assert err is not None
        assert "forbidden" in err.lower()

    def test_pythonpath_env_rejected(self):
        err = _validate_mcp_command("python3", ["mcp_servers/memory_server.py"],
                                     {"PYTHONPATH": "/tmp/evil"})
        assert err is not None

    def test_non_string_arg_rejected(self):
        err = _validate_mcp_command("python3", [None], {})
        assert err is not None

    def test_non_string_env_value_rejected(self):
        err = _validate_mcp_command("python3", ["mcp_servers/memory_server.py"],
                                     {"FOO": 12345})
        assert err is not None

    def test_empty_command_rejected(self):
        err = _validate_mcp_command("", [], {})
        assert err is not None

    def test_whitespace_command_rejected(self):
        err = _validate_mcp_command("   ", [], {})
        assert err is not None


class TestValidateMcpCommandAllowsLegit:
    def test_python3_with_project_script_allowed(self):
        # The legitimate use: a built-in MCP server script run by python3
        err = _validate_mcp_command(
            "python3",
            ["mcp_servers/memory_server.py"],
            {},
        )
        assert err is None, f"expected allowed, got: {err}"

    def test_uv_with_mcp_script_allowed(self):
        err = _validate_mcp_command(
            "uv", ["run", "mcp_servers/rag_server.py"], {},
        )
        assert err is None

    def test_npx_with_mcp_package_allowed(self):
        err = _validate_mcp_command(
            "npx", ["-y", "@modelcontextprotocol/server-filesystem"], {},
        )
        assert err is None

    def test_absolute_path_inside_project_allowed(self):
        # An absolute path to a real file inside the project root should pass
        err = _validate_mcp_command(
            "/Users/ernest/Desktop/DEVPROJECTS/odysseus/mcp_servers/email_server.py",
            [],
            {},
        )
        assert err is None, f"expected allowed, got: {err}"

    def test_env_with_safe_keys_allowed(self):
        err = _validate_mcp_command(
            "python3", ["mcp_servers/memory_server.py"],
            {"DEBUG": "1", "LOG_LEVEL": "info", "MCP_NAME": "memory"},
        )
        assert err is None
