#!/usr/bin/env python3
"""
Reset demo tenants for Véktor.

Eliminates all 3 demo tenants (cascade) and re-seeds from scratch.
Useful for resetting between investor demos.

Usage:
    make reset-demo
    python scripts/reset_demo_data.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # scripts/

from seed_demo_data import main  # noqa: E402

if __name__ == "__main__":
    asyncio.run(main(reset=True))
