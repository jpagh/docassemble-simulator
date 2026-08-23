"""Local docassemble interview simulator.

Loads a docassemble package's interview in-process (no server), runs the real
seek/assemble machinery, and exposes a CLI that humans and AI agents can use
to step through screens, answer fields, and debug variable-resolution errors.
"""

from docassemble_simulator.cli import main

__all__ = ["main"]
