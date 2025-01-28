from typing import Any, List, Type

from firebolt.utils.exception import ConnectionClosedError

ASYNC_QUERY_STATUS_RUNNING = "RUNNING"
ASYNC_QUERY_STATUS_SUCCESSFUL = "ENDED_SUCCESSFULLY"
ASYNC_QUERY_STATUS_REQUEST = "CALL fb_GetAsyncStatus('{token}')"


class BaseConnection:
    def __init__(self, cursor_type: Type) -> None:
        self.cursor_type = cursor_type
        self._cursors: List[Any] = []
        self._is_closed = False

    def _remove_cursor(self, cursor: Any) -> None:
        # This way it's atomic
        try:
            self._cursors.remove(cursor)
        except ValueError:
            pass

    @property
    def closed(self) -> bool:
        """`True` if connection is closed; `False` otherwise."""
        return self._is_closed

    def commit(self) -> None:
        """Does nothing since Firebolt doesn't have transactions."""

        if self.closed:
            raise ConnectionClosedError("Unable to commit: Connection closed.")
