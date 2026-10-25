from importlib.metadata import PackageNotFoundError, version

# The distribution is garmin-mirror-mcp (the import package stays garmin_mcp).
try:
    __version__ = version("garmin-mirror-mcp")
except PackageNotFoundError:  # running from a checkout without an install
    __version__ = "0.0.0"
