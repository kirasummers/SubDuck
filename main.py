#!/usr/bin/env python3
import sys
import os

# Ensure package directory is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from subduck.gui import main

if __name__ == "__main__":
    main()
