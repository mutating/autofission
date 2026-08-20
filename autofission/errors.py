"""Autofission-specific exceptions."""


class AutofissionError(Exception):
    """Base class for expected Autofission failures."""


class ConfigurationError(AutofissionError):
    """Raised when command-line or environment configuration is invalid."""


class CapacityError(AutofissionError):
    """Raised when Kubernetes capacity data cannot be used safely."""


class KubernetesProtocolError(AutofissionError):
    """Raised when the Kubernetes API returns an invalid list document."""


class ReconcileError(AutofissionError):
    """Raised after a reconciliation cycle with one or more item failures."""

    def __init__(self, failures: tuple[str, ...]) -> None:
        self.failures = failures
        super().__init__(
            f'{len(failures)} managed function(s) failed: {", ".join(failures)}',
        )
