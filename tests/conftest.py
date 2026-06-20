import sys
from pathlib import Path

# Allow `import hpc_cf` without installing the package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
