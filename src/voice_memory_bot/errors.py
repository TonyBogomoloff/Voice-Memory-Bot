class ProviderError(Exception):
    """Only a fixed error code escapes provider boundaries; never tokens, URLs or response bodies."""
