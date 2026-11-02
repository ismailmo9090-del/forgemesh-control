"""Console entry point module for ``fmc``.

``python -m forgemesh_control.main`` is the interface the desktop shell spawns,
so the module must actually run the CLI when executed directly.
"""

from forgemesh_control.cli import app

__all__ = ["app"]


if __name__ == "__main__":
    app()
