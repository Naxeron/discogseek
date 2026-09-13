"""
Main entry point for executing musicscraper as a module: python -m musicscraper
"""

import sys
from musicscraper.cli.main import main

if __name__ == "__main__":
    sys.exit(main())
