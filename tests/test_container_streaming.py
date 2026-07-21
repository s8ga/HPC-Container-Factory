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
from pathlib import Path
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


def test_run_streaming_keeps_bounded_tail_only() -> None:
    """Long streams retain only the configured byte budget in memory."""
    from hpc_cf import container as container_mod

    # Tiny budget so early lines fall out of the ring.
    old_max = container_mod.STREAM_TAIL_MAX_BYTES
    container_mod.STREAM_TAIL_MAX_BYTES = 40
    try:
        lines = [f"line-{i:04d}-xxxxxxxx" for i in range(20)]
        mock_proc = MagicMock()
        mock_proc.stdout = io.StringIO("\n".join(lines) + "\n")
        mock_proc.wait.return_value = 0

        ctr = _make_container()
        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch.object(logging.getLogger("hpc_cf.container"), "info"),
        ):
            result = ctr._run(["exec", "test", "true"])

        assert "line-0000" not in result.stdout
        assert "line-0019" in result.stdout
        assert len(result.stdout.encode("utf-8")) <= 40 + 20  # small slack
    finally:
        container_mod.STREAM_TAIL_MAX_BYTES = old_max


def test_run_failure_exception_includes_last_kb() -> None:
    """CalledProcessError.output is capped to STREAM_ERROR_TAIL_BYTES."""
    from hpc_cf import container as container_mod

    old_err = container_mod.STREAM_ERROR_TAIL_BYTES
    old_max = container_mod.STREAM_TAIL_MAX_BYTES
    container_mod.STREAM_TAIL_MAX_BYTES = 10_000
    container_mod.STREAM_ERROR_TAIL_BYTES = 32
    try:
        body = ("A" * 200) + "\n" + ("B" * 200) + "\nERROR-TAIL\n"
        mock_proc = MagicMock()
        mock_proc.stdout = io.StringIO(body)
        mock_proc.wait.return_value = 7

        ctr = _make_container()
        with patch("subprocess.Popen", return_value=mock_proc):
            with pytest.raises(subprocess.CalledProcessError) as exc_info:
                ctr._run(["build", "-t", "x", "."])

        out = exc_info.value.output
        assert "ERROR-TAIL" in out
        assert len(out.encode("utf-8")) <= 32 + 8
        assert "A" * 50 not in out
    finally:
        container_mod.STREAM_ERROR_TAIL_BYTES = old_err
        container_mod.STREAM_TAIL_MAX_BYTES = old_max


def test_run_streaming_optional_log_file(tmp_path: Path) -> None:
    """Optional stream_log_path receives the full line stream."""
    log_path = tmp_path / "podman.stream.log"
    mock_proc = MagicMock()
    mock_proc.stdout = io.StringIO("one\ntwo\nthree\n")
    mock_proc.wait.return_value = 0

    ctr = Container(name="t", image="i", stream_log_path=log_path)
    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch.object(logging.getLogger("hpc_cf.container"), "info"),
    ):
        ctr._run(["ps"])

    text = log_path.read_text(encoding="utf-8")
    assert text.splitlines() == ["one", "two", "three"]


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
