"""The published interface contract.

Other projects parse this CLI's `--json` output and branch on its exit codes, so
both are versioned promises rather than implementation details:

* add keys to a payload freely; renaming or removing one is a breaking change
  and requires bumping `SCHEMA_VERSION`.
* exit codes distinguish *why* a command failed, so a caller can decide whether
  retrying could help (gateway down: yes, later; no data stored: no, not until
  something syncs).

Nothing IBKR-specific belongs here, and nothing here may import the IB client:
consumers of the contract must be able to read it without a Gateway.
"""

from __future__ import annotations

SCHEMA_VERSION = 1

# Exit codes. 0/1/2/130 match long-standing shell convention; 3 and 4 split the
# two failures that used to be indistinguishable behind a bare 1.
EXIT_OK = 0
EXIT_ERROR = 1  # unexpected runtime failure
EXIT_USAGE = 2  # argparse rejected the arguments
EXIT_GATEWAY = 3  # Gateway unreachable / not logged in - retry may help
EXIT_NO_DATA = 4  # nothing stored or nothing configured yet - retry will not help
EXIT_NEEDS_2FA = 5  # login is waiting for a 6-digit code only the human can supply
EXIT_INTERRUPT = 130  # SIGINT


class NoData(LookupError):
    """Asked for stored data that does not exist yet.

    Subclasses LookupError so existing `except LookupError` handlers keep
    working.
    """
