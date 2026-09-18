"""Entry point for `python -m teams_refresher` and for the login services.

The services are given this file's absolute path rather than a `-m` line,
because a command string is all any of systemd, launchd and the Run key
accept -- there is nowhere to set a working directory or PYTHONPATH that
works the same on all three. Running the file directly leaves __package__
empty, so put the project root on sys.path before importing ourselves.
"""

import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from teams_refresher.cli import main
else:
    from .cli import main

if __name__ == "__main__":
    sys.exit(main())
