"""Pytest fixtures for kindling-bonfires tests. Sets OPENROUTER_API_KEY before importing kindling."""

import os
import sys
from pathlib import Path

# Ensure kindling-bonfires is on path and env is set before any kindling import
sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("OPENROUTER_API_KEY", "test-key-for-unit-tests")
