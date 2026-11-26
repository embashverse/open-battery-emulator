"""UART interface. The engine loads it by name ("uart" under interfaces: in config.yaml)
and calls only these four functions (interface contract in PROJECT.md)."""

from interfaces.uart.uart_port import read_current, send, start, stop

__all__ = ["read_current", "send", "start", "stop"]
