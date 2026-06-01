"""Unit tests for _pip_install_snippet — the status-preserving pip install shell helper.

These tests exercise the helper in isolation without importing the full
cookbook_routes module (which pulls in FastAPI, the DB layer, etc.).
The function is re-implemented inline so the test stays self-contained.
"""

import re
import subprocess
import sys
import unittest


def _pip_install_snippet(pip_args: str) -> str:
    """Mirror of routes.cookbook_routes._pip_install_snippet for testing."""
    return (
        f"_pip_out=$(mktemp) && "
        f"python3 -m pip install {pip_args} >\"$_pip_out\" 2>&1; _pip_ec=$?; "
        f"tail -5 \"$_pip_out\"; rm -f \"$_pip_out\"; exit $_pip_ec"
    )


class TestPipInstallSnippet(unittest.TestCase):
    """Verify the generated shell snippet preserves pip's real exit code."""

    def test_contains_temp_file_capture(self):
        s = _pip_install_snippet("-q huggingface_hub")
        self.assertIn("mktemp", s)
        self.assertIn('>"$_pip_out"', s)

    def test_saves_exit_code_immediately(self):
        s = _pip_install_snippet("-q huggingface_hub")
        self.assertIn("_pip_ec=$?", s)

    def test_shows_tail_for_diagnostics(self):
        s = _pip_install_snippet("-q huggingface_hub")
        self.assertIn('tail -5 "$_pip_out"', s)

    def test_cleans_up_temp_file(self):
        s = _pip_install_snippet("-q huggingface_hub")
        self.assertIn('rm -f "$_pip_out"', s)

    def test_exits_with_pip_status(self):
        s = _pip_install_snippet("-q huggingface_hub")
        self.assertIn("exit $_pip_ec", s)

    def test_injects_pip_args(self):
        s = _pip_install_snippet("--user --break-system-packages -q hf_transfer")
        self.assertIn("--user --break-system-packages -q hf_transfer", s)

    def test_pip_failure_propagates(self):
        """Run the snippet with a deliberately broken pip install and verify
        the subshell exits non-zero (i.e. the || chain would fire)."""
        snippet = _pip_install_snippet("no-such-package-xyzzy-12345")
        result = subprocess.run(
            ["bash", "-c", snippet],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0,
                            "pip install of a non-existent package should exit non-zero")

    def test_tail_output_on_failure(self):
        """When pip fails, the last 5 lines of its output should appear on
        stdout so the tmux log stays useful."""
        snippet = _pip_install_snippet("no-such-package-xyzzy-12345")
        result = subprocess.run(
            ["bash", "-c", snippet],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # pip prints "ERROR:" on failure — we should see at least some of it
        output = result.stdout.lower()
        self.assertTrue(
            "error" in output or "no matching" in output or "could not" in output,
            f"Expected pip error output in stdout, got: {result.stdout!r}",
        )

    def test_no_pipe_in_snippet(self):
        """The snippet must NOT contain a bare | tail — that's the bug it fixes."""
        s = _pip_install_snippet("-q huggingface_hub")
        # The only pipe-like thing should be inside the redirect (which is >, not |)
        self.assertNotRegex(s, r"\|\s*tail")


if __name__ == "__main__":
    unittest.main()
