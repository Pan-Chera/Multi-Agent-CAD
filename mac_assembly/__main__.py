"""Make `python -m mac_assembly` work (the README-documented entry point).

Delegates to mac_assembly.graph_assembly.main.
"""
import sys

from mac_assembly.graph_assembly import main

if __name__ == "__main__":
    sys.exit(main())
