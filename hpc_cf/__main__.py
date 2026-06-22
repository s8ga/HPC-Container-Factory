"""Entry point: ``python -m hpc_cf``."""

from __future__ import annotations

import logging
import subprocess
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def main() -> None:
    from hpc_cf.cli import build_parser, run_new_cli

    argv = sys.argv[1:]
    if not argv:
        parser = build_parser()
        parser.print_help()
        print("\nQuick start:")
        print("  python -m hpc_cf dockerfile --app-version cp2k_rocm-2026.1-gfx942")
        print("  python -m hpc_cf build --app-version cp2k_rocm-2026.1-gfx942")
        print("  python -m hpc_cf assets --env cp2k_rocm-2026.1-gfx942")
        print("  python -m hpc_cf build-sif --app-version cp2k_opensource-2026.1-force-avx512")
        print("  python -m hpc_cf pack-apptainer")
        sys.exit(0)

    try:
        code = run_new_cli(argv)
    except (FileNotFoundError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        msg = str(exc)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stdout:
            msg += f"\n--- output (last 2000 chars) ---\n{exc.stdout[-2000:]}"
        logging.getLogger(__name__).error(msg)
        sys.exit(1)

    sys.exit(code)


if __name__ == "__main__":
    main()
