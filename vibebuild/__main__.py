"""Allow running vibebuild as ``python -m vibebuild``."""
from vibebuild.cli import main
import sys

sys.exit(main())
