"""Outbound relay Worker for the local Subtitle Cleanup Service.

The package deliberately treats the public relay as an untrusted network until
its configured CA validates the HTTPS connection.  Runtime secrets are loaded
only from ignored local configuration or VSR_RELAY_* environment variables.
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
