"""Allow `python -m vibebuild`."""
from vibebuild.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
