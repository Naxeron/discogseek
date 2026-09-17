"""
Main entry point for executing discogseek as a module: python -m discogseek
"""

import sys
from discogseek.cli.main import main

if __name__ == "__main__":
    sys.exit(main())
