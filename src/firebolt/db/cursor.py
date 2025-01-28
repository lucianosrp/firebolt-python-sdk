from __future__ import annotations

import logging
import time
from abc import ABCMeta, abstractmethod
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Generator,
    List,
    Optional,
    Sequence,
    Union,
)
from urllib.parse import urljoin

from httpx import (
    URL,
    USE_CLIENT_DEFAULT,
    Headers,
    Response,
    TimeoutException,
    codes,
)

from firebolt.client import Client, ClientV1, ClientV2
from firebolt.common._types import (
    ColType,
    ParameterType,
    SetParameter,
    split_format_sql,
)
from firebolt.common.base_cursor import (
    JSON_OUTPUT_FORMAT,
    RESET_SESSION_HEADER,
    UPDATE_ENDPOINT_HEADER,
    UPDATE_PARAMETERS_HEADER,
    BaseCursor,
    CursorState,
    _parse_update_endpoint,
    _parse_update_parameters,
    _raise_if_internal_set_parameter,
    async_not_allowed,
    check_not_closed,
    check_query_executed,
)
from firebolt.utils.exception import (
    EngineNotRunningError,
    FireboltDatabaseError,
    FireboltError,
    NotSupportedError,
    OperationalError,
    ProgrammingError,
    QueryTimeoutError,
)
from firebolt.utils.timeout_controller import TimeoutController
from firebolt.utils.urls import DATABASES_URL, ENGINES_URL
from firebolt.utils.util import _print_error_body, raise_errors_from_body

if TYPE_CHECKING:
    from firebolt.db.connection import Connection

logger = logging.getLogger(__name__)


class Cursor(BaseCursor, metaclass=ABCMeta):
    """
    Class, responsible for executing queries to Firebolt Database.
    Should not be created directly,
    use :py:func:`connection.cursor <firebolt.async_db.connection.Connection>`

    Args:
        description: Information about a single result row
        rowcount: The number of rows produced by last query
        closed: True if connection is closed, False otherwise
        arraysize: Read/Write, specifies the number of rows to fetch at a time
            with the :py:func:`fetchmany` method
    """

    def __init__(
        self,
        *args: Any,
        client: Client,
        connection: Connection,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._client = client
        self.connection = connection
        self.engine_url = connection.engine_url
        if connection.init_parameters:
            self._update_set_parameters(connection.init_parameters)

    def _raise_if_error(self, resp: Response) -> None:
        """Raise a proper error if any"""
        if resp.status_code == codes.INTERNAL_SERVER_ERROR:
            raise OperationalError(
                f"Error executing query:\n{resp.read().decode('utf-8')}"
            )
        if resp.status_code == codes.FORBIDDEN:
            if self.database and not self.is_db_available(self.database):
                raise FireboltDatabaseError(
                    f"Database {self.parameters['database']} does not exist"
                )
            raise ProgrammingError(resp.read().decode("utf-8"))
        if (
            resp.status_code == codes.SERVICE_UNAVAILABLE
            or resp.status_code == codes.NOT_FOUND
        ) and not self.is_engine_running(self.engine_url):
            raise EngineNotRunningError(
                f"Firebolt engine {self.engine_name} "
                "needs to be running to run queries against it."  # pragma: no mutate # noqa: E501
            )
        raise_errors_from_body(resp)
        # If no structure for error is found, log the body and raise the error
        _print_error_body(resp)
        resp.raise_for_status()

    def _api_request(
        self,
        query: str = "",
        parameters: Optional[dict[str, Any]] = None,
        path: str = "",
        use_set_parameters: bool = True,
        timeout: Optional[float] = None,
    ) -> Response:
        """
        Query API, return Response object.

        Args:
            query (str): SQL query
            parameters (Optional[Sequence[ParameterType]]): A sequence of substitution
                parameters. Used to replace '?' placeholders inside a query with
                actual values. Note: In order to "output_format" dict value, it
                    must be an empty string. If no value not specified,
                    JSON_OUTPUT_FORMAT will be used.
            path (str): endpoint suffix, for example "cancel" or "status"
            use_set_parameters: Optional[bool]: Some queries will fail if additional
                set parameters are sent. Setting this to False will allow
                self._set_parameters to be ignored.
            timeout (Optional[float]): Request execution timeout in seconds
        """
        parameters = parameters or {}
        if use_set_parameters:
            parameters = {**(self._set_parameters or {}), **parameters}
        if self.parameters:
            parameters = {**self.parameters, **parameters}
        try:
            return self._client.post(
                url=urljoin(self.engine_url.rstrip("/") + "/", path or ""),
                params=parameters,
                content=query,
                timeout=timeout if timeout is not None else USE_CLIENT_DEFAULT,
            )
        except TimeoutException:
            raise QueryTimeoutError()

    def _validate_set_parameter(
        self, parameter: SetParameter, timeout: Optional[float]
    ) -> None:
        """Validate parameter by executing simple query with it."""
        _raise_if_internal_set_parameter(parameter)
        resp = self._api_request(
            "select 1", {parameter.name: parameter.value}, timeout=timeout
        )
        # Handle invalid set parameter
        if resp.status_code == codes.BAD_REQUEST:
            raise OperationalError(resp.text)
        self._raise_if_error(resp)

        # set parameter passed validation
        self._set_parameters[parameter.name] = parameter.value

    def _parse_response_headers(self, headers: Headers) -> None:
        if headers.get(UPDATE_ENDPOINT_HEADER):
            endpoint, params = _parse_update_endpoint(
                headers.get(UPDATE_ENDPOINT_HEADER)
            )
            self._update_set_parameters(params)
            self.engine_url = endpoint

        if headers.get(RESET_SESSION_HEADER):
            self.flush_parameters()

        if headers.get(UPDATE_PARAMETERS_HEADER):
            param_dict = _parse_update_parameters(headers.get(UPDATE_PARAMETERS_HEADER))
            self._update_set_parameters(param_dict)

    @abstractmethod
    def execute_async(
        self,
        query: str,
        parameters: Optional[Sequence[ParameterType]] = None,
        skip_parsing: bool = False,
    ) -> int:
        """Execute a database query without maintaining a connection."""
        ...

    def _do_execute(
        self,
        raw_query: str,
        parameters: Sequence[Sequence[ParameterType]],
        skip_parsing: bool = False,
        timeout: Optional[float] = None,
        async_execution: bool = False,
    ) -> None:
        self._reset()
        queries: List[Union[SetParameter, str]] = (
            [raw_query] if skip_parsing else split_format_sql(raw_query, parameters)
        )
        timeout_controller = TimeoutController(timeout)

        if len(queries) > 1 and async_execution:
            raise FireboltError(
                "Server side async does not support multi-statement queries"
            )
        try:
            for query in queries:
                self._execute_single_query(query, timeout_controller, async_execution)
            self._state = CursorState.DONE
        except Exception:
            self._state = CursorState.ERROR
            raise

    def _execute_single_query(
        self,
        query: Union[SetParameter, str],
        timeout_controller: TimeoutController,
        async_execution: bool,
    ) -> None:
        start_time = time.time()
        Cursor._log_query(query)
        timeout_controller.raise_if_timeout()

        if isinstance(query, SetParameter):
            if async_execution:
                raise FireboltError(
                    "Server side async does not support set statements, "
                    "please use execute to set this parameter"
                )
            self._validate_set_parameter(query, timeout_controller.remaining())
        else:
            self._handle_query_execution(query, timeout_controller, async_execution)

        if not async_execution:
            logger.info(
                f"Query fetched {self.rowcount} rows in"
                f" {time.time() - start_time} seconds."
            )
        else:
            logger.info("Query submitted for async execution.")

    def _handle_query_execution(
        self, query: str, timeout_controller: TimeoutController, async_execution: bool
    ) -> None:
        query_params: Dict[str, Any] = {"output_format": JSON_OUTPUT_FORMAT}
        if async_execution:
            query_params["async"] = True
        resp = self._api_request(
            query,
            query_params,
            timeout=timeout_controller.remaining(),
        )
        self._raise_if_error(resp)
        if async_execution:
            self._parse_async_response(resp)
        else:
            self._parse_response_headers(resp.headers)
            row_set = self._row_set_from_response(resp)
            self._append_row_set(row_set)

    @check_not_closed
    def execute(
        self,
        query: str,
        parameters: Optional[Sequence[ParameterType]] = None,
        skip_parsing: bool = False,
        timeout_seconds: Optional[float] = None,
    ) -> Union[int, str]:
        """Prepare and execute a database query.

        Supported features:
            Parameterized queries: placeholder characters ('?') are substituted
                with values provided in `parameters`. Values are formatted to
                be properly recognized by database and to exclude SQL injection.
            Multi-statement queries: multiple statements, provided in a single query
                and separated by semicolon, are executed separatelly and sequentially.
                To switch to next statement result, `nextset` method should be used.
            SET statements: to provide additional query execution parameters, execute
                `SET param=value` statement before it. All parameters are stored in
                cursor object until it's closed. They can also be removed with
                `flush_parameters` method call.

        Args:
            query (str): SQL query to execute
            parameters (Optional[Sequence[ParameterType]]): A sequence of substitution
                parameters. Used to replace '?' placeholders inside a query with
                actual values
            skip_parsing (bool): Flag to disable query parsing. This will
                disable parameterized, multi-statement and SET queries,
                while improving performance
            timeout_seconds (Optional[float]): Query execution timeout in seconds

        Returns:
            int: Query row count.
        """
        params_list = [parameters] if parameters else []
        self._do_execute(query, params_list, skip_parsing, timeout=timeout_seconds)
        return self.rowcount

    @check_not_closed
    def executemany(
        self,
        query: str,
        parameters_seq: Sequence[Sequence[ParameterType]],
        timeout_seconds: Optional[float] = None,
    ) -> Union[int, str]:
        """Prepare and execute a database query.

        Supports providing multiple substitution parameter sets, executing them
        as multiple statements sequentially.

        Supported features:
            Parameterized queries: Placeholder characters ('?') are substituted
                with values provided in `parameters`. Values are formatted to
                be properly recognized by database and to exclude SQL injection.
            Multi-statement queries: Multiple statements, provided in a single query
                and separated by semicolon, are executed separately and sequentially.
                To switch to next statement result, use `nextset` method.
            SET statements: To provide additional query execution parameters, execute
                `SET param=value` statement before it. All parameters are stored in
                cursor object until it's closed. They can also be removed with
                `flush_parameters` method call.

        Args:
            query (str): SQL query to execute.
            parameters_seq (Sequence[Sequence[ParameterType]]): A sequence of
               substitution parameter sets. Used to replace '?' placeholders inside a
               query with actual values from each set in a sequence. Resulting queries
               for each subset are executed sequentially.
            timeout_seconds (Optional[float]): Query execution timeout in seconds.

        Returns:
            int: Query row count.
        """
        self._do_execute(query, parameters_seq, timeout=timeout_seconds)
        return self.rowcount

    @abstractmethod
    def is_db_available(self, database: str) -> bool:
        """Verify that the database exists."""
        ...

    @abstractmethod
    def is_engine_running(self, engine_url: str) -> bool:
        """Verify that the engine is running."""
        ...

    # Iteration support
    @check_not_closed
    @async_not_allowed
    @check_query_executed
    def __iter__(self) -> Generator[List[ColType], None, None]:
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row

    @check_not_closed
    def __enter__(self) -> Cursor:
        return self


class CursorV2(Cursor):
    def __init__(
        self, *args: Any, client: Client, connection: Connection, **kwargs: Any
    ) -> None:
        assert isinstance(client, ClientV2)  # Type check
        super().__init__(*args, client=client, connection=connection, **kwargs)

    def is_db_available(self, database_name: str) -> bool:
        """
        Verify that the database exists.

        Args:
            connection (firebolt.db.connection.Connection)
            database_name (str): Name of a database
        """
        # For v2 accounts if we're connected it automatically
        # means the database is available
        return True

    def is_engine_running(self, engine_url: str) -> bool:
        """
        Verify that the engine is running.

        Args:
            connection (firebolt.db.connection.Connection): connection.
            engine_url (str): URL of the engine
        """

        # For v2 accounts we don't have the engine context,
        # so we can't check if it's running
        return True

    @check_not_closed
    def execute_async(
        self,
        query: str,
        parameters: Optional[Sequence[ParameterType]] = None,
        skip_parsing: bool = False,
    ) -> int:
        """
        Execute a database query without maintating a connection.

        Supported features:
            Parameterized queries: placeholder characters ('?') are substituted
                with values provided in `parameters`. Values are formatted to
                be properly recognized by database and to exclude SQL injection.

        Not supported:
            Multi-statement queries: multiple statements, provided in a single query
                and separated by semicolon.
            SET statements: to provide additional query execution parameters, execute
                `SET param=value` statement before it. Use `execute` method to set
                parameters.

        Args:
            query (str): SQL query to execute
            parameters (Optional[Sequence[ParameterType]]): A sequence of substitution
                parameters. Used to replace '?' placeholders inside a query with
                actual values
            skip_parsing (bool): Flag to disable query parsing. This will
                disable parameterized queries while potentially improving performance

        Returns:
            int: Always returns -1, as async execution does not return row count.
        """
        self._do_execute(
            query,
            [parameters] if parameters else [],
            skip_parsing,
            async_execution=True,
        )
        return -1


class CursorV1(Cursor):
    def __init__(
        self, *args: Any, client: ClientV1, connection: Connection, **kwargs: Any
    ) -> None:
        assert isinstance(client, ClientV1)  # Type check
        super().__init__(*args, client=client, connection=connection, **kwargs)

    def is_db_available(self, database_name: str) -> bool:
        """
        Verify that the database exists.

        Args:
            connection (firebolt.async_db.connection.Connection)
        """
        resp = self._filter_request(
            DATABASES_URL, {"filter.name_contains": database_name}
        )
        return len(resp.json()["edges"]) > 0

    def is_engine_running(self, engine_url: str) -> bool:
        """
        Verify that the engine is running.

        Args:
            connection (firebolt.async_db.connection.Connection): connection.
        """
        # Url is not guaranteed to be of this structure,
        # but for the sake of error checking this is sufficient.
        engine_name = URL(engine_url).host.split(".")[0].replace("-", "_")
        resp = self._filter_request(
            ENGINES_URL,
            {
                "filter.name_contains": engine_name,
                "filter.current_status_eq": "ENGINE_STATUS_RUNNING_REVISION_SERVING",
            },
        )
        return len(resp.json()["edges"]) > 0

    def _filter_request(self, endpoint: str, filters: dict) -> Response:
        resp = self.connection._client.request(
            # Full url overrides the client url, which contains engine as a prefix.
            url=self.connection.api_endpoint + endpoint,
            method="GET",
            params=filters,
        )
        resp.raise_for_status()
        return resp

    def execute_async(
        self,
        query: str,
        parameters: Optional[Sequence[ParameterType]] = None,
        skip_parsing: bool = False,
    ) -> int:
        raise NotSupportedError(
            "Async execution is not supported in this version " " of Firebolt."
        )
