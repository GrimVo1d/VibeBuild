"""Allow running vibebuild as ``python -m vibebuild``."""

import sys

from vibebuild.cli import main

sys.exit(main())
