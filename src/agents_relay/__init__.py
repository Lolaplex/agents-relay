"""HTTP + Telegram relay to runner.loop."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("agents-relay")
except PackageNotFoundError:  # a source checkout that was never installed
    __version__ = "0.0.0+source"
