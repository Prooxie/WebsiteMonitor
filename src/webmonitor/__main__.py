"""Entry point: ``python -m webmonitor`` or the ``webmonitor`` console script."""

from __future__ import annotations

import sys

from webmonitor.app import run

__all__ = ["main"]


def main() -> int:
    """Run the application.

    Returns:
        The process exit code.
    """
    return run()


if __name__ == "__main__":
    sys.exit(main())
