"""Entry point usable from any working directory:  .venv\\Scripts\\python.exe server.py [serve|index|search|status]"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from faravex.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
