"""Minimal stubs — weather monitor doesn't use these exceptions."""


class GammaRateLimitError(Exception):
    """Stub for Gamma API rate limit error."""
    pass


class CLOBAuthenticationError(Exception):
    """Stub for CLOB authentication error."""
    pass


class CLOBRateLimitError(Exception):
    """Stub for CLOB rate limit error."""
    pass


class OrderRejectedError(Exception):
    """Stub for order rejection error."""
    pass
