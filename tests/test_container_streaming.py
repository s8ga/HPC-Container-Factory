"""W1: Container._run streaming — output must appear line-by-line, not buffered.

The old implementation used ``subprocess.run(capture_output=True, check=True)``
which had two critical bugs:

1. **Buffered**: all output collected until process exit — user sees nothing
   during long-running spack commands.
2. **Hidden on failure**: ``check=True`` raises *before* the re-emit block,
   so the actual error output is silently discarded.

The fix: when ``capture=False`` (the default for long-running commands like
exec, run_ephemeral, build_image), use ``Popen`` + line iteration to stream
output in real-time via the logger.
"""
from __future__ import annotations

import io
import logging
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from hpc_cf.container import Container


def _make_container() -> Container:
    """Create a Container without touching podman."""
    return Container(name="test-ctr", image="test-img")


# ── Streaming mode (capture=False) ──────────────────────────────────────────


def test_run_streams_line_by_line() -> None:
    """Each line of stdout appears via logger.info in real-time."""
    mock_proc = MagicMock()
    mock_proc.stdout = io.StringIO("line1\nline2\nline3\n")
    mock_proc.wait.return_value = 0

    ctr = _make_container()
    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch.object(logging.getLogger("hpc_cf.container"), "info") as mock_info,
    ):
        result = ctr._run(["exec", "test-ctr", "bash", "-lc", "echo hi"])

    # logger.info called for preamble + each output line
    podman_lines = [
        call.args[1] for call in mock_info.call_args_list
        if call.args and call.args[0] == "[podman] %s"
    ]
    assert podman_lines == ["line1", "line2", "line3"]
    assert result.returncode == 0
    assert result.stdout == "line1\nline2\nline3"


def test_run_streams_empty_output() -> None:
    """No crash when the command produces zero output."""
    mock_proc = MagicMock()
    mock_proc.stdout = io.StringIO("")
    mock_proc.wait.return_value = 0

    ctr = _make_container()
    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch.object(logging.getLogger("hpc_cf.container"), "info"),
    ):
        result = ctr._run(["ps"])

    assert result.returncode == 0
    assert result.stdout == ""


def test_run_failure_output_preserved_in_exception() -> None:
    """On failure, streamed output is included in CalledProcessError.output."""
    mock_proc = MagicMock()
    mock_proc.stdout = io.StringIO("building...\nError: compilation failed\n")
    mock_proc.wait.return_value = 1

    ctr = _make_container()
    with patch("subprocess.Popen", return_value=mock_proc):
        with pytest.raises(subprocess.CalledProcessError) as exc_info:
            ctr._run(["build", "-t", "test", "."])

    assert exc_info.value.returncode == 1
    assert "Error: compilation failed" in exc_info.value.output
    assert "building..." in exc_info.value.output


def test_run_failure_streams_before_raising() -> None:
    """Output is streamed via logger even when the command ultimately fails."""
    mock_proc = MagicMock()
    mock_proc.stdout = io.StringIO("step 1 ok\nstep 2 FAIL\n")
    mock_proc.wait.return_value = 42

    ctr = _make_container()
    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch.object(logging.getLogger("hpc_cf.container"), "info") as mock_info,
    ):
        with pytest.raises(subprocess.CalledProcessError):
            ctr._run(["exec", "test-ctr", "bash", "-lc", "false"])

    podman_lines = [
        call.args[1] for call in mock_info.call_args_list
        if call.args and call.args[0] == "[podman] %s"
    ]
    assert "step 1 ok" in podman_lines
    assert "step 2 FAIL" in podman_lines


def test_run_check_false_no_raise_on_failure() -> None:
    """check=False returns CompletedProcess with non-zero returncode."""
    mock_proc = MagicMock()
    mock_proc.stdout = io.StringIO("partial output\n")
    mock_proc.wait.return_value = 1

    ctr = _make_container()
    with patch("subprocess.Popen", return_value=mock_proc):
        result = ctr._run(["ps", "-q"], check=False)

    assert result.returncode == 1
    assert result.stdout == "partial output"


def test_run_strips_trailing_newlines() -> None:
    """Output lines don't carry trailing \\n into logger or CompletedProcess."""
    mock_proc = MagicMock()
    mock_proc.stdout = io.StringIO("foo\n\nbar\n")
    mock_proc.wait.return_value = 0

    ctr = _make_container()
    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch.object(logging.getLogger("hpc_cf.container"), "info") as mock_info,
    ):
        ctr._run(["images"])

    podman_lines = [
        call.args[1] for call in mock_info.call_args_list
        if call.args and call.args[0] == "[podman] %s"
    ]
    # Empty lines are skipped
    assert podman_lines == ["foo", "bar"]


def test_run_uses_popen_not_subprocess_run_when_streaming() -> None:
    """capture=False must go through Popen (streaming), not subprocess.run."""
    mock_proc = MagicMock()
    mock_proc.stdout = io.StringIO("ok\n")
    mock_proc.wait.return_value = 0

    ctr = _make_container()
    with (
        patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
        patch("subprocess.run") as mock_run,
    ):
        ctr._run(["exec", "test", "echo", "hi"])

    mock_popen.assert_called_once()
    mock_run.assert_not_called()


# ── Buffered mode (capture=True) ────────────────────────────────────────────


def test_run_capture_uses_subprocess_run() -> None:
    """capture=True must use subprocess.run (buffered), not Popen."""
    expected = subprocess.CompletedProcess(
        args=["mock"], returncode=0, stdout="captured", stderr="",
    )
    ctr = _make_container()
    with (
        patch("subprocess.run", return_value=expected) as mock_run,
        patch("subprocess.Popen") as mock_popen,
    ):
        result = ctr._run(["inspect", "-f", "{{.Id}}", "test-img"], capture=True)

    mock_run.assert_called_once()
    mock_popen.assert_not_called()
    assert result.stdout == "captured"


def test_run_capture_check_false() -> None:
    """capture=True with check=False suppresses CalledProcessError."""
    expected = subprocess.CompletedProcess(
        args=["mock"], returncode=1, stdout="", stderr="not found",
    )
    ctr = _make_container()
    with patch("subprocess.run", return_value=expected):
        result = ctr._run(["image", "exists", "missing"], capture=True, check=False)

    assert result.returncode == 1


# ── Real subprocess smoke test (no podman needed) ───────────────────────────


def test_run_streams_real_echo() -> None:
    """End-to-end: _run streams output from a real 'echo' command.

    Uses a bare Container whose podman_cmd is 'echo' so no podman binary is
    needed. Verifies the Popen path works for a real process.
    """
    ctr = Container(name="x", image="x", podman_cmd="echo")
    with (
        patch.object(logging.getLogger("hpc_cf.container"), "info") as mock_info,
    ):
        result = ctr._run(["hello", "world"])

    assert result.returncode == 0
    podman_lines = [
        call.args[1] for call in mock_info.call_args_list
        if call.args and call.args[0] == "[podman] %s"
    ]
    assert "hello world" in podman_lines
