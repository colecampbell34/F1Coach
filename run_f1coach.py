from __future__ import annotations

import sys

from f1coach.cli import main


if __name__ == "__main__":
    args = sys.argv[1:] or ["dashboard", "--open-browser", "--show-packets"]
    raise SystemExit(main(args))
