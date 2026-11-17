"""CAN interface. The engine loads it by name ("can" under interfaces: in config.yaml)
and calls only these four functions (interface contract in PROJECT.md)."""

from interfaces.can.can_bus import read_current, send, start, stop

__all__ = ["read_current", "send", "start", "stop"]
