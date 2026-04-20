#!/bin/bash
# Activate local apptainer — add to PATH
#
# Usage:
#   source ./activate-apptainer.sh
#
# This script auto-detects the apptainer/ directory next to itself
# and adds its bin/ to your PATH (idempotently).
#
# When the makeself archive is extracted, this script is also run once
# automatically to show a usage hint.

_this_file="$(realpath "${BASH_SOURCE[0]}" 2>/dev/null || readlink -f "${BASH_SOURCE[0]}")"
_this_dir="${_this_file%/*}"

# The makeself archive extracts to: <target>/apptainer/
# The layout is:
#   <target>/apptainer/bin/apptainer     (wrapper)
#   <target>/apptainer/x86_64/           (actual binaries)
#   <target>/activate-apptainer.sh       (this file)
_apptainer_bin="$_this_dir/apptainer/bin"

if [ ! -x "$_apptainer_bin/apptainer" ]; then
    echo "⚠ apptainer binary not found at $_apptainer_bin/apptainer" >&2
    echo "  Expected layout:" >&2
    echo "    $_this_dir/apptainer/bin/apptainer" >&2
    return 2>/dev/null || exit 1
fi

# If sourced (not executed by makeself), add to PATH
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
    case ":${PATH}:" in
        *":$_apptainer_bin:"*)
            ;;
        *)
            export PATH="$_apptainer_bin:$PATH"
            echo "✅ apptainer added to PATH ($("$_apptainer_bin/apptainer" --version 2>/dev/null || echo 'version unknown'))"
            ;;
    esac
else
    # Executed directly by makeself after extraction — just print a hint
    echo ""
    echo "Apptainer extracted to: $_this_dir/apptainer/"
    echo ""
    echo "To activate, run:"
    echo "  source $_this_dir/activate-apptainer.sh"
    echo ""
    echo "Then use:"
    echo "  apptainer shell /path/to/image.sif"
    echo "  apptainer exec /path/to/image.sif cp2k.psmp ..."
    echo ""
fi
