#!/bin/bash
# Quick activation script for the HPC Container Factory development environment
# Usage: source activate.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/venv/bin/activate"

# Add local apptainer to PATH (installed by generate.py build-sif)
APPTAINER_LOCAL="$SCRIPT_DIR/tools/apptainer/bin"
if [ -d "$APPTAINER_LOCAL" ]; then
    case ":${PATH}:" in
        *":$APPTAINER_LOCAL:"*) ;;  # already in PATH
        *) export PATH="$APPTAINER_LOCAL:$PATH" ;;
    esac
fi

echo "Activated HPC Container Factory environment"
