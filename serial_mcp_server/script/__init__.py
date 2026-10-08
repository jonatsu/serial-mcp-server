"""The script engine: parse a script, then run it against a port session.

Nothing in this package imports ``mcp``, so the command-line runner can use it without the server.
"""

from serial_mcp_server.script.directives import DIRECTIVES, Directive
from serial_mcp_server.script.model import Script, ScriptError, ScriptSyntaxError, Step, StepKind
from serial_mcp_server.script.parser import parse

__all__ = [
    "DIRECTIVES",
    "Directive",
    "Script",
    "ScriptError",
    "ScriptSyntaxError",
    "Step",
    "StepKind",
    "parse",
]
