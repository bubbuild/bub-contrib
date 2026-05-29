from __future__ import annotations


class WorkflowError(ValueError):
    """Base error for workflow planning, coordination, and execution."""


class WorkflowSpecError(WorkflowError):
    """Raised when a workflow specification is invalid."""


class WorkflowStateError(WorkflowError):
    """Raised when persisted workflow state cannot be used."""


class WorkflowExecutionError(WorkflowError):
    """Raised when a workflow run fails before producing task state."""
