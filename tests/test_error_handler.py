"""W4: Top-level error handler must include CalledProcessError output.

The old handler printed only ``str(exc)``, which for CalledProcessError
produces ``"Command 'podman ...' returned non-zero exit status 1"`` —
no diagnostic content from stdout/stderr.
"""
from __future__ import annotations

import subprocess

import pytest


def test_main_includes_called_process_error_stdout(caplog, monkeypatch) -> None:
    """CalledProcessError output must appear in the error log."""
    err = subprocess.CalledProcessError(
        returncode=1,
        cmd=["podman", "exec"],
        output="Fetching...\nError: package not found",
    )

    def fake_run_cli(argv):
        raise err

    monkeypatch.setattr("hpc_cf.cli.run_new_cli", fake_run_cli)
    monkeypatch.setattr("sys.argv", ["hpc_cf", "assets"])

    from hpc_cf.__main__ import main

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 1
    assert any("Error: package not found" in r.message for r in caplog.records)


def test_main_includes_called_process_error_truncation(caplog, monkeypatch) -> None:
    """Very long output is truncated to last 2000 chars."""
    long_output = "A" * 5000 + "UNIQUE_TAIL_MARKER"
    err = subprocess.CalledProcessError(
        returncode=1, cmd=["podman"], output=long_output,
    )

    monkeypatch.setattr("hpc_cf.cli.run_new_cli", lambda argv: (_ for _ in ()).throw(err))
    monkeypatch.setattr("sys.argv", ["hpc_cf", "build"])

    from hpc_cf.__main__ import main

    with pytest.raises(SystemExit):
        main()

    logged = " ".join(r.message for r in caplog.records)
    assert "UNIQUE_TAIL_MARKER" in logged
    assert "A" * 5000 not in logged  # truncated, not full


def test_main_runtime_error_passthrough(caplog, monkeypatch) -> None:
    """Non-CalledProcessError exceptions still work (str(exc) only)."""
    monkeypatch.setattr("hpc_cf.cli.run_new_cli", lambda argv: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr("sys.argv", ["hpc_cf", "assets"])

    from hpc_cf.__main__ import main

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 1
    assert any("boom" in r.message for r in caplog.records)
