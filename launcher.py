"""Entry point for the Windows executable (PyInstaller)."""
import sys

from observer.app import main

if __name__ == "__main__":
    sys.exit(main())
