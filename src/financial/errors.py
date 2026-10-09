from __future__ import annotations


class NotFoundError(Exception):
    """Maps to HTTP 404 at the API boundary."""


class RevisionConflictError(Exception):
    """expected_revision didn't match current state. Maps to HTTP 409."""


class ValidationError(Exception):
    """Maps to HTTP 422 at the API boundary."""


class DuplicateFileError(Exception):
    """Same (account, sha256) as an already-committed import. Maps to 409."""

    def __init__(self, existing_import_id: str):
        super().__init__(f"Duplicate of already-committed import: {existing_import_id}")
        self.existing_import_id = existing_import_id


class InvalidImportStateError(Exception):
    """The requested action doesn't apply to the import's current state. Maps to 409."""

    def __init__(self, state: str):
        super().__init__(f"Import is not in a valid state for this action: {state}")
        self.state = state


class UnresolvedRowsError(Exception):
    """Confirm was called with rows still needing an explicit decision. Maps to 422."""

    def __init__(self, count: int):
        super().__init__(f"{count} row(s) still need a decision before this import can commit")
        self.count = count
