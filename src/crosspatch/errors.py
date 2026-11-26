"""Errors that end a command with a message and a stable exit code."""


class CrossError(Exception):
    """A failure the user can act on. The message is shown without a traceback."""

    code = 64


class UsageError(CrossError):
    code = 64


class DataError(CrossError):
    """The input is well formed but not allowed, such as a message that holds a secret."""

    code = 65


class Unavailable(CrossError):
    code = 69


class TimedOut(CrossError):
    """Nothing arrived in time. Callers print how to keep waiting."""

    code = 2
