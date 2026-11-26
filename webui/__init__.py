"""Optional web UI: live values and test controls in a browser (REQ-15).

The engine loads this package by name, only when webui.enabled is true, and
calls the two functions of the web UI contract in docs/engineering/design.md:

    start(host, port, handle)   start the server thread, return the port used
    stop()                      stop it (called once on exit)
"""

from webui.server import start, stop

__all__ = ["start", "stop"]
