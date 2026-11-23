"""Application-specific exceptions."""


class AlphaMacroError(Exception):
    """Base class for all application errors."""


class ConfigurationError(AlphaMacroError):
    """Environment/configuration is missing or invalid."""


class InvalidQueryError(AlphaMacroError):
    """User input failed validation."""


class MarketDataError(AlphaMacroError):
    """Market data could not be retrieved."""


class AgentExecutionError(AlphaMacroError):
    """The research agent failed to produce an answer."""


class CoinNotFoundError(MarketDataError):
    """The requested coin id does not exist at the data provider."""


class PolicyViolationError(InvalidQueryError):
    """Input was rejected by a safety/compliance policy (e.g. it contains a secret)."""
