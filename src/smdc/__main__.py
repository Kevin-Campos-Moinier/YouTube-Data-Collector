"""Allows `python -m smdc`."""

import sys

from .cli import main

sys.exit(main())
