import os
import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, Mock, call, patch

from google.auth.exceptions import RefreshError

from credentials import (
    hydrate_job_settings,
    load_active_gsc_credentials,
    load_user_credentials,
    mark_gsc_reconnect_required,
)
from models import JobRow, JobSettings, RunJobRequest
from routers import intro, jobs
from utils import gsc


SERVICE_ACCOUNT = {
    "method": "service_account",
    "service_account": {"client_email": "runtime@example.com", "private_key": "runtime-private-key"},
}
OAUTH = {"method": "google_oauth", "refresh_token_ciphertext": "v1:runtime-ciphertext"}
RECONNECT_ERROR = "Google Search Console reconnect required."
UNAVAILABLE_ERROR = "Selected Google Search Console connection unavailable."
SECRETS = ("runtime-api-secret", "runtime-dfs-secret", "v1:runtime-ciphertext", "runtime-private-key")


class _Response:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, sb, table):
        self.sb = sb
        self.table = table
        self.filters = []
        self.in_filters = []
        self.operation = "select"
        self.payload = None

    def select(self, _columns):
        return self

    def insert(self, payload):
        self.operation, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.operation, self.payload = "update", payload
        return self

    def eq(self, column, value):
        self.filters.append((column, value))
        return self

    def in_(self, column, values):
        self.in_filters.append((column, tuple(values)))
        return self

    def execute(self):
        self.sb.executed.append(self)
        source = self.sb.tables.get(self.table, [])
        if isinstance(source, Exception):
            raise source
        if self.operation == "insert":
            return _Response([{"id": "job-new", **self.payload}])
        rows = [
            row for row in source
            if all(row.get(key) == value for key, value in self.filters)
            and all(row.get(key) in values for key, values in self.in_filters)
        ]
        if self.operation == "update":
            for row in rows:
                row.update(self.payload)
        return _Response(rows)


class _Supabase:
    def __init__(self, tables=None):
        self.tables = tables or {}
        self.executed = []

    def table(self, name):
        return _Query(self, name)


class _BackgroundTasks:
    def __init__(self):
        self.calls = []

    def add_task(self, function, *args, **kwargs):
        self.calls.append((function, args, kwargs))


class _DatabaseError(Exception):
    def __init__(self, code):
        self.code = code


def _tables(method="service_account", oauth_status="connected"):
    return {
        "user_settings": [{
            "user_id": "user-1",
            "gsc_auth_method": method,
            "provider_settings": {"api_key": "runtime-api-secret", "dfs_password": "runtime-dfs-secret"},
        }],
        "user_credentials": [{
            "user_id": "user-1",
            "provider_settings": {},
            "gsc_service_account": SERVICE_ACCOUNT["service_account"],
        }],
        "gsc_oauth_connections": [{
            "user_id": "user-1",
            "status": oauth_status,
            "refresh_token_ciphertext": OAUTH["refresh_token_ciphertext"],
        }],
        "jobs": [],
    }


def _runtime_settings(envelope=OAUTH):
    return {
        "provider": "Claude",
        "api_key": "runtime-api-secret",
        "dfs_password": "runtime-dfs-secret",
        "use_gsc": True,
        "site_url": "sc-domain:example.com",
        "_gsc_credentials": envelope,
    }


def _stored_job(error=None):
    row = {
        "id": "job-1",
        "user_id": "user-1",
        "settings": {"provider": "Claude", "use_gsc": True, "site_url": "sc-domain:example.com"},
        "rows": [{"url": "https://example.com/page", "keyword": "manual"}],
        "results": [{}],
    }
    if error is not None:
        row["error"] = error
    return row


def _assert_persistence_is_secret_free(test_case, sb):
    payloads = repr([query.payload for query in sb.executed if query.payload is not None])
    for secret in SECRETS:
        test_case.assertNotIn(secret, payloads)
    test_case.assertNotIn("_gsc_credentials", payloads)
    test_case.assertNotIn("_gsc_service_account", payloads)


class CredentialSelectionTests(unittest.TestCase):
    def test_get_and_duplicate_strip_legacy_secrets_without_mutating_source(self):
        legacy_settings = {
            "provider": "Claude", "api_key": "legacy-api", "dfs_password": "legacy-dfs",
            "jina_api_key": "legacy-jina", "gsc_service_account": {"private_key": "legacy-gsc"},
            "_gsc_credentials": {"refresh_token_ciphertext": "legacy-oauth"},
            "_gsc_service_account": {"private_key": "legacy-runtime-gsc"},
        }
        source = {**_stored_job(), "name": "Legacy", "settings": legacy_settings}
        sb = _Supabase({"jobs": [source]})
        with patch.object(jobs, "get_supabase", return_value=sb):
            response = jobs.get_job("job-1", user=SimpleNamespace(id="user-1"))
        with patch.object(jobs, "enforce_rate_limit"):
            jobs.duplicate_job("job-1", user=SimpleNamespace(id="user-1"), sb=sb)
        self.assertEqual(response["settings"], {"provider": "Claude"})
        insert = [query for query in sb.executed if query.operation == "insert"][-1]
        self.assertEqual(insert.payload["settings"], {"provider": "Claude"})
        self.assertEqual(source["settings"], legacy_settings)

    def test_selector_supports_both_authoritative_modes(self):
        for method, expected in (("service_account", SERVICE_ACCOUNT), ("google_oauth", OAUTH)):
            with self.subTest(method=method):
                self.assertEqual(load_active_gsc_credentials(_Supabase(_tables(method)), "user-1"), expected)

    def test_selector_missing_invalid_and_inactive_never_falls_back(self):
        cases = [
            ("google_oauth", "reconnect_required"),
            ("invalid_method", "connected"),
        ]
        for method, status in cases:
            with self.subTest(method=method, status=status):
                self.assertIsNone(load_active_gsc_credentials(_Supabase(_tables(method, status)), "user-1"))

        tables = _tables("service_account")
        tables["user_credentials"][0]["gsc_service_account"] = None
        self.assertIsNone(load_active_gsc_credentials(_Supabase(tables), "user-1"))

    def test_only_recognized_server_credential_migration_errors_are_ignored(self):
        for code in ("PGRST204", "PGRST205", "42P01", "42703"):
            tables = _tables()
            tables["user_credentials"] = _DatabaseError(code)
            self.assertEqual(load_user_credentials(_Supabase(tables), "user-1")["provider_settings"]["api_key"], "runtime-api-secret")

        tables = _tables()
        tables["user_credentials"] = _DatabaseError("50000")
        with self.assertRaises(_DatabaseError):
            load_user_credentials(_Supabase(tables), "user-1")

    def test_hydration_strips_all_incoming_secrets_then_uses_server_selection(self):
        incoming = {
            "provider": "Claude",
            "api_key": "attacker-api",
            "dfs_password": "attacker-dfs",
            "jina_api_key": "attacker-jina",
            "_gsc_service_account": {"private_key": "attacker-key"},
            "_gsc_credentials": {"method": "google_oauth", "refresh_token_ciphertext": "attacker-token"},
        }
        hydrated = hydrate_job_settings(_Supabase(_tables("service_account")), "user-1", incoming)
        self.assertEqual(hydrated["_gsc_credentials"], SERVICE_ACCOUNT)
        self.assertEqual(hydrated["api_key"], "runtime-api-secret")
        self.assertNotIn("_gsc_service_account", hydrated)
        self.assertNotIn("attacker", repr(hydrated))

    def test_reconnect_marker_is_tenant_status_and_ciphertext_stale_safe(self):
        tables = _tables("google_oauth")
        sb = _Supabase(tables)
        self.assertTrue(mark_gsc_reconnect_required(sb, "user-1", OAUTH["refresh_token_ciphertext"]))
        query = sb.executed[-1]
        self.assertEqual(query.filters, [
            ("user_id", "user-1"),
            ("status", "connected"),
            ("refresh_token_ciphertext", OAUTH["refresh_token_ciphertext"]),
        ])
        self.assertEqual(query.payload["last_error_code"], "refresh_failed")

        for user_id, ciphertext in (("other-user", OAUTH["refresh_token_ciphertext"]), ("user-1", "v1:stale")):
            self.assertFalse(mark_gsc_reconnect_required(_Supabase(_tables("google_oauth")), user_id, ciphertext))


class GscClientTests(unittest.TestCase):
    def test_scope_and_service_account_alias_are_exact(self):
        self.assertEqual(gsc.GSC_SCOPES, ["https://www.googleapis.com/auth/webmasters.readonly"])
        with patch.object(gsc, "ServiceAccountCredentials", create=True) as credentials, patch.object(gsc, "build") as build:
            gsc.get_gsc_client(SERVICE_ACCOUNT)
        credentials.from_service_account_info.assert_called_once_with(
            SERVICE_ACCOUNT["service_account"], scopes=["https://www.googleapis.com/auth/webmasters.readonly"]
        )
        build.assert_called_once_with("searchconsole", "v1", credentials=credentials.from_service_account_info.return_value)

    def test_oauth_reads_env_before_decrypt_then_refreshes_before_build(self):
        order = Mock()
        credentials = Mock()
        request = Mock()
        with (
            patch.dict(os.environ, {"GOOGLE_OAUTH_CLIENT_ID": "client-id", "GOOGLE_OAUTH_CLIENT_SECRET": "client-secret"}, clear=True),
            patch.object(gsc, "decrypt_secret", return_value="refresh-token", create=True) as decrypt,
            patch.object(gsc, "OAuthCredentials", return_value=credentials, create=True) as oauth_credentials,
            patch.object(gsc, "Request", return_value=request, create=True),
            patch.object(gsc, "build") as build,
        ):
            order.attach_mock(decrypt, "decrypt")
            order.attach_mock(oauth_credentials, "credentials")
            order.attach_mock(credentials.refresh, "refresh")
            order.attach_mock(build, "build")
            gsc.get_gsc_client({**OAUTH, "client_id": "ignored", "client_secret": "ignored"})
        self.assertEqual(order.mock_calls, [
            call.decrypt(OAUTH["refresh_token_ciphertext"]),
            call.credentials(token=None, refresh_token="refresh-token", token_uri=gsc.TOKEN_URI, client_id="client-id", client_secret="client-secret", scopes=gsc.GSC_SCOPES),
            call.refresh(request),
            call.build("searchconsole", "v1", credentials=credentials),
        ])

    def test_oauth_sanitizes_env_values_before_building_credentials(self):
        credentials = Mock()
        with (
            patch.dict(os.environ, {"GOOGLE_OAUTH_CLIENT_ID": "\ufeffclient-id\n", "GOOGLE_OAUTH_CLIENT_SECRET": " client-secret\t"}, clear=True),
            patch.object(gsc, "decrypt_secret", return_value="refresh-token", create=True),
            patch.object(gsc, "OAuthCredentials", return_value=credentials, create=True) as oauth_credentials,
            patch.object(gsc, "Request", create=True),
            patch.object(gsc, "build"),
        ):
            gsc.get_gsc_client(OAUTH)

        self.assertEqual(oauth_credentials.call_args.kwargs["client_id"], "client-id")
        self.assertEqual(oauth_credentials.call_args.kwargs["client_secret"], "client-secret")

    def test_missing_env_precedes_decrypt_and_invalid_envelopes_are_safe(self):
        with patch.dict(os.environ, {"GOOGLE_OAUTH_CLIENT_SECRET": "secret"}, clear=True), patch.object(gsc, "decrypt_secret", create=True) as decrypt:
            with self.assertRaisesRegex(gsc.GscOAuthConfigError, "Google OAuth configuration is incomplete"):
                gsc.get_gsc_client(OAUTH)
            decrypt.assert_not_called()

        for envelope in (None, "private", {}, {"method": "service_account"}, {"method": "google_oauth"}):
            with self.subTest(envelope=envelope), self.assertRaisesRegex(ValueError, "^Invalid GSC credentials$"):
                gsc.get_gsc_client(envelope)


class RuntimePathTests(unittest.TestCase):
    def test_jobs_background_rerun_helpers_require_user_id(self):
        for function in (jobs._rerun_single_row, jobs._rerun_multiple_rows):
            with self.subTest(function=function.__name__):
                parameter = inspect.signature(function).parameters["user_id"]
                self.assertIs(parameter.default, inspect.Parameter.empty)

    def test_rerun_routes_scope_scheduling_updates_to_user(self):
        for is_bulk in (False, True):
            with self.subTest(is_bulk=is_bulk):
                sb = _Supabase({"jobs": [
                    {**_stored_job(), "user_id": "other-user"},
                    _stored_job(),
                ]})
                background = _BackgroundTasks()
                with (
                    patch.object(jobs, "enforce_job_start"),
                    patch.object(jobs, "enforce_rate_limit"),
                    patch.object(jobs, "execute_active_job_write", side_effect=lambda write, _tool: write()),
                ):
                    if is_bulk:
                        jobs.rerun_rows(
                            "job-1",
                            jobs.MultiRerunRequest(row_indices=[0]),
                            background,
                            user=SimpleNamespace(id="user-1"),
                            sb=sb,
                        )
                    else:
                        jobs.rerun_row(
                            "job-1",
                            0,
                            background_tasks=background,
                            user=SimpleNamespace(id="user-1"),
                            sb=sb,
                        )

                job_queries = [query for query in sb.executed if query.table == "jobs"]
                for query in job_queries:
                    self.assertEqual(query.filters, [
                        ("id", "job-1"),
                        ("user_id", "user-1"),
                    ])
                self.assertNotIn("current_step", sb.tables["jobs"][0])

    def test_single_rerun_scopes_every_job_query_and_threads_user_id(self):
        sb = _Supabase({"jobs": [
            {**_stored_job(), "user_id": "other-user", "results": [{"status": "other"}]},
            _stored_job(),
        ]})
        settings = {**_runtime_settings(), "use_gsc": False}
        with (
            patch.object(jobs, "hydrate_job_settings", return_value=settings),
            patch.object(intro, "_process_single_row", autospec=True, return_value={"status": "ok"}) as process,
        ):
            jobs._rerun_single_row(
                "job-1",
                0,
                _stored_job()["rows"],
                _stored_job()["settings"],
                sb,
                user_id="user-1",
            )

        self.assertEqual(process.call_args.kwargs["user_id"], "user-1")
        for query in (query for query in sb.executed if query.table == "jobs"):
            self.assertEqual(query.filters[:2], [
                ("id", "job-1"),
                ("user_id", "user-1"),
            ])
        self.assertEqual(sb.tables["jobs"][0]["results"], [{"status": "other"}])

    def test_bulk_rerun_scopes_every_job_query_and_threads_user_id(self):
        sb = _Supabase({"jobs": [
            {**_stored_job(), "user_id": "other-user", "results": [{"status": "other"}]},
            _stored_job(),
        ]})
        settings = {**_runtime_settings(), "use_gsc": False}
        with (
            patch.object(jobs, "hydrate_job_settings", return_value=settings),
            patch.object(intro, "_process_single_row", autospec=True, return_value={"status": "ok"}) as process,
        ):
            jobs._rerun_multiple_rows(
                "job-1",
                [0],
                _stored_job()["rows"],
                _stored_job()["settings"],
                sb,
                "user-1",
            )

        self.assertEqual(process.call_args.kwargs["user_id"], "user-1")
        for query in (query for query in sb.executed if query.table == "jobs"):
            self.assertEqual(query.filters[:2], [
                ("id", "job-1"),
                ("user_id", "user-1"),
            ])
        self.assertEqual(sb.tables["jobs"][0]["results"], [{"status": "other"}])

    def test_intro_persistence_helpers_require_user_id(self):
        for function in (
            intro._is_cancelled,
            intro._process_single_row,
            intro._process_job,
            intro._update_job,
        ):
            with self.subTest(function=function.__name__):
                parameter = inspect.signature(function).parameters["user_id"]
                self.assertIs(parameter.default, inspect.Parameter.empty)

    def test_intro_job_reads_and_updates_are_tenant_scoped(self):
        sb = _Supabase({"jobs": [
            {"id": "job-1", "user_id": "other-user", "status": "cancelling", "logs": []},
            {"id": "job-1", "user_id": "user-1", "status": "running", "logs": []},
        ]})

        self.assertFalse(intro._is_cancelled(sb, "job-1", "user-1"))
        intro._update_job(sb, "job-1", "user-1", {"current_step": "Scoped update"})

        job_queries = [query for query in sb.executed if query.table == "jobs"]
        self.assertEqual(len(job_queries), 3)
        for query in job_queries:
            self.assertEqual(query.filters, [
                ("id", "job-1"),
                ("user_id", "user-1"),
            ])
        self.assertNotIn("current_step", sb.tables["jobs"][0])
        self.assertEqual(sb.tables["jobs"][1]["current_step"], "Scoped update")

    def test_initial_hydration_failure_returns_fixed_503_before_persistence(self):
        sb = _Supabase(_tables("google_oauth"))
        background = _BackgroundTasks()
        request = RunJobRequest(
            name="Runtime",
            rows=[JobRow(url="https://example.com/page")],
            settings=JobSettings(use_gsc=True),
        )

        with (
            patch.object(intro, "get_supabase", return_value=sb),
            patch.object(intro, "enforce_job_start"),
            patch.object(intro, "enforce_rate_limit"),
            patch.object(intro, "execute_active_job_write") as write,
            patch.object(intro, "hydrate_job_settings", side_effect=RuntimeError("private database detail")),
        ):
            with self.assertRaises(intro.HTTPException) as raised:
                intro.run_intro_job(request, background, user=SimpleNamespace(id="user-1"))

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(
            raised.exception.detail,
            "Saved credentials are temporarily unavailable. Please try again.",
        )
        self.assertIsNone(raised.exception.__cause__)
        write.assert_not_called()
        self.assertEqual(background.calls, [])
        self.assertFalse(any(query.operation == "insert" for query in sb.executed))

    def test_successful_single_and_bulk_retry_clear_only_credential_error(self):
        cases = (
            (jobs._rerun_single_row, None),
            (jobs._rerun_multiple_rows, [0]),
        )
        for function, indices in cases:
            for existing_error, expected_error in (
                ("Saved credentials are temporarily unavailable.", None),
                ("Unrelated job failure", "Unrelated job failure"),
                (RECONNECT_ERROR, RECONNECT_ERROR),
            ):
                with self.subTest(function=function.__name__, existing_error=existing_error):
                    sb = _Supabase({"jobs": [_stored_job(existing_error)]})
                    settings = {**_runtime_settings(), "use_gsc": False}
                    with (
                        patch.object(jobs, "hydrate_job_settings", return_value=settings),
                        patch.object(intro, "_process_single_row", return_value={"status": "ok"}),
                        patch.object(intro, "_update_job"),
                    ):
                        if indices is None:
                            function(
                                "job-1",
                                0,
                                _stored_job()["rows"],
                                _stored_job()["settings"],
                                sb,
                                user_id="user-1",
                            )
                        else:
                            function(
                                "job-1",
                                indices,
                                _stored_job()["rows"],
                                _stored_job()["settings"],
                                sb,
                                "user-1",
                            )

                    self.assertEqual(sb.tables["jobs"][0].get("error"), expected_error)
                    clear_queries = [
                        query for query in sb.executed
                        if query.operation == "update" and query.payload == {"error": None}
                    ]
                    self.assertEqual(len(clear_queries), 1)
                    self.assertEqual(clear_queries[0].filters, [
                        ("id", "job-1"),
                        ("user_id", "user-1"),
                    ])
                    self.assertEqual(clear_queries[0].in_filters, [(
                        "error",
                        ("Saved credentials are temporarily unavailable.",),
                    )])

    def test_single_rerun_hydration_failure_persists_safe_tenant_scoped_failure(self):
        private_detail = "database-password-private-detail"
        sb = _Supabase({"jobs": [{**_stored_job(), "status": "complete"}]})

        with (
            patch.object(jobs, "hydrate_job_settings", side_effect=RuntimeError(private_detail)),
            patch.object(intro, "_process_single_row") as process,
        ):
            jobs._rerun_single_row(
                "job-1",
                0,
                _stored_job()["rows"],
                _stored_job()["settings"],
                sb,
                user_id="user-1",
            )

        process.assert_not_called()
        update = [query for query in sb.executed if query.operation == "update"][-1]
        self.assertEqual(update.filters, [("id", "job-1"), ("user_id", "user-1")])
        self.assertEqual(update.payload, {
            "error": "Saved credentials are temporarily unavailable.",
            "current_step": "Row 1 re-run failed: saved credentials are temporarily unavailable.",
            "updated_at": "now()",
        })
        self.assertNotIn("rerunning", update.payload["current_step"].lower())
        self.assertNotIn(private_detail, repr(update.payload))

    def test_bulk_rerun_hydration_failure_sets_terminal_safe_tenant_scoped_failure(self):
        private_detail = "database-token-private-detail"
        sb = _Supabase({"jobs": [{**_stored_job(), "status": "running"}]})

        with (
            patch.object(jobs, "hydrate_job_settings", side_effect=RuntimeError(private_detail)),
            patch.object(intro, "_process_single_row") as process,
        ):
            jobs._rerun_multiple_rows(
                "job-1",
                [0],
                _stored_job()["rows"],
                _stored_job()["settings"],
                sb,
                "user-1",
            )

        process.assert_not_called()
        update = [query for query in sb.executed if query.operation == "update"][-1]
        self.assertEqual(update.filters, [("id", "job-1"), ("user_id", "user-1")])
        self.assertEqual(update.payload, {
            "status": "failed",
            "error": "Saved credentials are temporarily unavailable.",
            "current_step": "Re-run failed: saved credentials are temporarily unavailable.",
            "updated_at": "now()",
        })
        self.assertNotIn(private_detail, repr(update.payload))

    def test_rerun_hydration_failure_swallows_persistence_errors(self):
        for function, indices in (
            (jobs._rerun_single_row, None),
            (jobs._rerun_multiple_rows, [0]),
        ):
            with self.subTest(function=function.__name__):
                sb = _Supabase({"jobs": RuntimeError("database update unavailable")})
                with (
                    patch.object(jobs, "hydrate_job_settings", side_effect=RuntimeError("hydrate unavailable")),
                    patch.object(intro, "_process_single_row") as process,
                ):
                    if indices is None:
                        function(
                            "job-1",
                            0,
                            _stored_job()["rows"],
                            _stored_job()["settings"],
                            sb,
                            "user-1",
                        )
                    else:
                        function(
                            "job-1",
                            indices,
                            _stored_job()["rows"],
                            _stored_job()["settings"],
                            sb,
                            "user-1",
                        )
                process.assert_not_called()

    def test_rerun_failure_payloads_do_not_persist_exception_secrets(self):
        sentinel = "SENTINEL-RERUN-SECRET"
        for function, indices in (
            (jobs._rerun_single_row, None),
            (jobs._rerun_multiple_rows, [0]),
        ):
            with self.subTest(function=function.__name__):
                sb = _Supabase({"jobs": [_stored_job()]})
                settings = {**_runtime_settings(), "use_gsc": False}
                with (
                    patch.object(jobs, "hydrate_job_settings", return_value=settings),
                    patch.object(intro, "_process_single_row", side_effect=RuntimeError(sentinel)),
                ):
                    if indices is None:
                        function(
                            "job-1",
                            0,
                            _stored_job()["rows"],
                            _stored_job()["settings"],
                            sb,
                            "user-1",
                        )
                    else:
                        function(
                            "job-1",
                            indices,
                            _stored_job()["rows"],
                            _stored_job()["settings"],
                            sb,
                            "user-1",
                        )

                payloads = [query.payload for query in sb.executed if query.payload is not None]
                self.assertNotIn(sentinel, repr(payloads))
                if indices is None:
                    self.assertEqual(payloads[-1]["current_step"], "Row 1 failed. Please try again.")
                else:
                    self.assertEqual(payloads[-1]["results"][0]["error"], "Row re-run failed. Please try again.")

    def test_intro_background_failure_payload_does_not_persist_exception_secret(self):
        sentinel = "SENTINEL-INTRO-SECRET"
        sb = _Supabase({"jobs": [_stored_job()]})
        with (
            patch.object(intro, "get_supabase", return_value=sb),
            patch.object(intro, "_process_single_row", side_effect=RuntimeError(sentinel)),
        ):
            intro._process_job(
                "job-1",
                _stored_job()["rows"],
                {**_runtime_settings(), "use_gsc": False},
                None,
                "user-1",
            )

        payloads = [query.payload for query in sb.executed if query.payload is not None]
        self.assertNotIn(sentinel, repr(payloads))
        result_updates = [payload for payload in payloads if payload.get("results")]
        self.assertEqual(
            result_updates[0]["results"][0]["error"],
            "Row processing failed. Please try again.",
        )

    def test_intro_generation_failure_result_does_not_expose_exception_secret(self):
        sentinel = "SENTINEL-GENERATION-SECRET"
        selection = {
            "primary": {"keyword": "manual", "volume": 100, "difficulty": 20},
            "supporting": [],
            "cluster_source": "manual",
            "runner_up": None,
        }
        settings = {
            **_runtime_settings(),
            "dfs_login": "login",
            "scrape_pages": False,
        }
        with (
            patch.object(intro, "get_ranked_keywords_for_page", return_value=[]),
            patch.object(intro, "get_keyword_overview", return_value={}),
            patch.object(intro, "get_keyword_difficulty", return_value={}),
            patch.object(intro, "select_intro_keywords", return_value=selection),
            patch.object(intro, "generate_intro", side_effect=RuntimeError(sentinel)),
        ):
            result = intro._process_single_row(
                row={"url": "https://example.com/page", "keyword": "manual"},
                settings=settings,
                gsc_client=None,
                branded_terms=[],
                used_primaries=set(),
                user_id="user-1",
            )

        self.assertEqual(result["error"], "Row processing failed. Please try again.")
        self.assertNotIn(sentinel, repr(result))

    def test_initial_path_uses_exact_envelope_and_never_persists_secrets(self):
        sb = _Supabase(_tables("google_oauth"))
        background = _BackgroundTasks()
        request = RunJobRequest(name="Runtime", rows=[JobRow(url="https://example.com/page")], settings=JobSettings(use_gsc=True))
        with (
            patch.object(intro, "get_supabase", return_value=sb),
            patch.object(intro, "enforce_job_start"),
            patch.object(intro, "enforce_rate_limit"),
            patch.object(intro, "execute_active_job_write", side_effect=lambda write, _tool: write()),
            patch.object(intro, "hydrate_job_settings", return_value=_runtime_settings()),
        ):
            intro.run_intro_job(request, background, user=SimpleNamespace(id="user-1"))
        function, args, kwargs = background.calls[0]
        self.assertIs(function, intro._process_job)
        self.assertEqual(args, ())
        self.assertEqual(kwargs["gsc_credentials"], OAUTH)
        self.assertEqual(kwargs["user_id"], "user-1")
        self.assertNotIn("sa_info", kwargs)
        _assert_persistence_is_secret_free(self, sb)

    def test_initial_processing_supports_both_modes_and_fixed_errors(self):
        for envelope in (SERVICE_ACCOUNT, OAUTH):
            with (
                self.subTest(method=envelope["method"]),
                patch.object(intro, "get_supabase", return_value=Mock()),
                patch.object(intro, "get_gsc_client", return_value="client") as get_client,
                patch.object(intro, "_process_single_row", return_value={"status": "ok"}) as process,
                patch.object(intro, "_update_job"),
                patch.object(intro, "_is_cancelled", return_value=False),
            ):
                intro._process_job("job-1", [{"url": "https://example.com/page"}], _runtime_settings(envelope), envelope, user_id="user-1")
                get_client.assert_called_once_with(envelope)
                self.assertEqual(process.call_args.kwargs["gsc_client"], "client")
                self.assertEqual(process.call_args.kwargs["gsc_auth_method"], envelope["method"])

        for failure, expected in ((RefreshError("provider detail"), RECONNECT_ERROR), (RuntimeError("provider detail"), UNAVAILABLE_ERROR)):
            updates = []
            with (
                patch.object(intro, "get_supabase", return_value=Mock()),
                patch.object(intro, "get_gsc_client", side_effect=failure),
                patch.object(intro, "mark_gsc_reconnect_required") as mark,
                patch.object(intro, "_process_single_row", return_value={"status": "ok"}),
                patch.object(
                    intro,
                    "_update_job",
                    side_effect=lambda _sb, _job, user_id, data: updates.append((user_id, data)),
                ),
                patch.object(intro, "_is_cancelled", return_value=False),
            ):
                intro._process_job("job-1", [{"url": "https://example.com/page"}], _runtime_settings(), OAUTH, user_id="user-1")
            self.assertIn(("user-1", {"error": expected}), updates)
            if isinstance(failure, RefreshError):
                mark.assert_called_once_with(ANY, "user-1", OAUTH["refresh_token_ciphertext"])

    def test_single_row_result_includes_safe_gsc_auth_method_label(self):
        selection = {
            "primary": {"keyword": "manual", "volume": 100, "difficulty": 20},
            "supporting": [],
            "cluster_source": "manual",
            "runner_up": None,
        }
        settings = {
            **_runtime_settings(),
            "dfs_login": "login",
            "scrape_pages": False,
        }
        with (
            patch.object(intro, "get_ranked_keywords_for_page", return_value=[]),
            patch.object(intro, "get_keyword_overview", return_value={}),
            patch.object(intro, "get_keyword_difficulty", return_value={}),
            patch.object(intro, "select_intro_keywords", return_value=selection),
            patch.object(intro, "generate_intro", return_value="Generated intro copy."),
        ):
            result = intro._process_single_row(
                row={"url": "https://example.com/page", "keyword": "manual"},
                settings=settings,
                gsc_client=None,
                gsc_auth_method="google_oauth",
                branded_terms=[],
                used_primaries=set(),
                user_id="user-1",
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["gsc_auth_method"], "google_oauth")

    def test_rerun_client_handles_missing_refresh_generic_and_exact_error_clear(self):
        cases = [
            ({"use_gsc": True}, None, UNAVAILABLE_ERROR),
            (_runtime_settings(), RefreshError("provider detail"), RECONNECT_ERROR),
            (_runtime_settings(), RuntimeError("provider detail"), UNAVAILABLE_ERROR),
        ]
        for settings, failure, expected in cases:
            with self.subTest(expected=expected):
                sb = _Supabase({"jobs": [_stored_job()]})
                with patch("utils.gsc.get_gsc_client", side_effect=failure), patch.object(jobs, "mark_gsc_reconnect_required") as mark:
                    result = jobs._get_runtime_gsc_client(settings, sb, "user-1", "job-1")
                self.assertIsNone(result)
                self.assertEqual(sb.tables["jobs"][0]["error"], expected)
                if isinstance(failure, RefreshError):
                    mark.assert_called_once_with(sb, "user-1", OAUTH["refresh_token_ciphertext"])

        for old_error, expected in ((RECONNECT_ERROR, None), (UNAVAILABLE_ERROR, None), ("Unrelated failure", "Unrelated failure")):
            sb = _Supabase({"jobs": [_stored_job(old_error)]})
            with patch("utils.gsc.get_gsc_client", return_value="client"):
                self.assertEqual(jobs._get_runtime_gsc_client(_runtime_settings(), sb, "user-1", "job-1"), "client")
            self.assertEqual(sb.tables["jobs"][0]["error"], expected)
            clear = [q for q in sb.executed if q.operation == "update" and q.payload == {"error": None}][0]
            self.assertEqual(clear.filters, [("id", "job-1"), ("user_id", "user-1")])
            self.assertEqual(clear.in_filters, [("error", (UNAVAILABLE_ERROR, RECONNECT_ERROR, jobs._GSC_CONFIG_ERROR))])

    def test_single_and_multi_reruns_freshly_hydrate_and_use_exact_envelope(self):
        for function, indices in ((jobs._rerun_single_row, None), (jobs._rerun_multiple_rows, [0])):
            for envelope in (SERVICE_ACCOUNT, OAUTH):
                with self.subTest(function=function.__name__, method=envelope["method"]):
                    sb = _Supabase({"jobs": [_stored_job()]})
                    with (
                        patch.object(jobs, "hydrate_job_settings", return_value=_runtime_settings(envelope)) as hydrate,
                        patch.object(jobs, "_get_runtime_gsc_client", return_value="client") as client,
                        patch.object(intro, "_process_single_row", return_value={"status": "ok"}) as process,
                        patch.object(intro, "_update_job"),
                    ):
                        if indices is None:
                            function("job-1", 0, _stored_job()["rows"], _stored_job()["settings"], sb, user_id="user-1")
                        else:
                            function("job-1", indices, _stored_job()["rows"], _stored_job()["settings"], sb, "user-1")
                    hydrate.assert_called_once_with(sb, "user-1", _stored_job()["settings"])
                    client.assert_called_once_with(_runtime_settings(envelope), sb, "user-1", "job-1")
                    self.assertEqual(process.call_args.kwargs["gsc_client"], "client")
                    _assert_persistence_is_secret_free(self, sb)


if __name__ == "__main__":
    unittest.main()
