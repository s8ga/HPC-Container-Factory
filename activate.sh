#!/bin/bash
# Quick activation script for the HPC Container Factory development environment
# Usage: source activate.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/venv/bin/activate"
echo "Activated HPC Container Factory environment"
