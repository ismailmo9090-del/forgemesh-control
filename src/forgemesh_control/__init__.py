"""ForgeMesh Control Center: AI multi-agent server orchestration.

The package is deliberately separate from :mod:`forgemesh_browser`: the browser
runtime stays an independent service that is consumed over its HTTP/CLI/WS
contract. This package owns orchestration, projects, the AI Council, workers and
the dashboard API.
"""

__version__ = "0.1.3"
