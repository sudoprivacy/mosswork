"""Single gate for the private Harness contracts used by this release."""

from importlib.metadata import version

SUPPORTED_HARNESS_VERSION = "0.0.1"


def require_supported_harness() -> None:
    actual = version("moss-harness")
    if actual != SUPPORTED_HARNESS_VERSION:
        raise RuntimeError(
            f"Unsupported moss-harness {actual}; this release requires {SUPPORTED_HARNESS_VERSION}"
        )
