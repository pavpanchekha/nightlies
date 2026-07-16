#!/usr/bin/env python3

from __future__ import annotations

import importlib
from pathlib import Path
import configparser
import gzip
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import urllib.error
import urllib.parse
from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest import mock

# Ensure direct execution (uv run test/test_nightlies.py) resolves project modules from repo root.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nightlies import NightlyRunner
import apt
import cli


class FakeRunner:
    def __init__(self, dryrun: bool = False) -> None:
        self.dryrun = dryrun
        self.logs: list[tuple[int, str]] = []
        self.commands: list[list[str]] = []

    def log(self, level: int, message: str) -> None:
        self.logs.append((level, message))

    def exec(self, level: int, cmd: list[str]) -> subprocess.CompletedProcess[bytes]:
        self.commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")


class FakeResponse:
    def __init__(self, data: bytes, status: int = 200) -> None:
        self.data = data
        self.status = status
        self.offset = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.data) - self.offset
        start = self.offset
        end = min(len(self.data), start + size)
        self.offset = end
        return self.data[start:end]

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Literal[False]:
        return False


class FakeOpener:
    def __init__(self, responses: dict[str, bytes]) -> None:
        self.responses = responses
        self.requests: list[str] = []

    def open(self, request: str | object) -> FakeResponse:
        if isinstance(request, str):
            url = request
        else:
            url = cast(str, getattr(request, "full_url"))
        self.requests.append(url)
        return FakeResponse(self.responses[url])


class TestApt(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="apt-test-"))
        self.source_list = self.tmpdir / "sources.list"
        self.sources_dir = self.tmpdir / "sources.list.d"
        self.sources_dir.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir)

    def test_add_repositories_skips_existing_ppa(self) -> None:
        (self.sources_dir / "owner-name.list").write_text(
            "deb https://ppa.launchpadcontent.net/owner/name/ubuntu noble main\n"
        )
        runner = FakeRunner()

        with mock.patch.object(apt, "_has_repository", return_value=True):
            failed = apt.add_repositories(cast(NightlyRunner, runner), ["ppa:owner/name"])

        self.assertEqual(failed, [])
        self.assertEqual(runner.commands, [])
        self.assertIn((1, "Apt repository ppa:owner/name already present; skipping"), runner.logs)

    def test_add_repositories_adds_missing_ppa(self) -> None:
        runner = FakeRunner()

        with mock.patch.object(apt, "_has_repository", return_value=False):
            failed = apt.add_repositories(cast(NightlyRunner, runner), ["ppa:owner/name"])

        self.assertEqual(failed, [])
        self.assertEqual(
            runner.commands,
            [["sudo", "add-apt-repository", "--yes", "ppa:owner/name"]],
        )

    def test_has_repository_matches_launchpad_sources(self) -> None:
        (self.source_list).write_text(
            "Types: deb\n"
            "URIs: https://ppa.launchpadcontent.net/owner/name/ubuntu\n"
            "Suites: noble\n"
            "Components: main\n"
        )

        self.assertTrue(apt._has_repository("ppa:owner/name", self.source_list, self.sources_dir))
        self.assertFalse(apt._has_repository("ppa:other/name", self.source_list, self.sources_dir))


class TestCli(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="cli-test-"))
        self.old_cwd = Path.cwd()
        self.env_patch = mock.patch.dict(os.environ, {"HOME": str(self.tmpdir)}, clear=False)
        self.env_patch.start()
        os.chdir(self.tmpdir)

    def tearDown(self) -> None:
        os.chdir(self.old_cwd)
        self.env_patch.stop()
        shutil.rmtree(self.tmpdir)

    def client_config(self, base_url: str = "https://nightly.cs.washington.edu/") -> cli.ClientConfig:
        return cli.ClientConfig(base_url, "uwplse", "uwplse")

    def absolute_url(self, client_config: cli.ClientConfig, path: str) -> str:
        return urllib.parse.urljoin(client_config.index_url, path)

    def report_url(self, client_config: cli.ClientConfig, path: str) -> str:
        return urllib.parse.urljoin(client_config.index_url, cli.REPORTS_PATH + path)

    def client_open_patch(self, opener: object) -> Any:
        return mock.patch.object(
            cli.ClientConfig,
            "open",
            autospec=True,
            side_effect=lambda _client_config, request: cast(Any, opener).open(request),
        )

    def fake_curl_run(
        self,
        responses: dict[str, bytes],
    ) -> mock.Mock:
        def run(cmd: list[str], check: bool) -> subprocess.CompletedProcess[bytes]:
            self.assertTrue(check)
            config_path = Path(cmd[cmd.index("--config") + 1])
            lines = [line.strip() for line in config_path.read_text().splitlines() if line.strip()]
            for i in range(0, len(lines), 2):
                url = lines[i].removeprefix('url = "').removesuffix('"')
                output = Path(lines[i + 1].removeprefix('output = "').removesuffix('"'))
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(responses[url])
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

        return mock.Mock(side_effect=run)

    def test_cmd_download_fetches_manifest_and_ungzips_files(self) -> None:
        report_name = "1713570000:taylor-order0:deadbeef"
        client_config = self.client_config()
        report_url = self.report_url(client_config, "herbie/" + report_name)
        manifest = {
            "files": [
                {"path": "index.html", "gzip": False},
                {"path": "nightly_info.json", "gzip": False},
                {"path": "results.json.gz", "gzip": True},
            ]
        }
        opener = FakeOpener(
            {
                self.absolute_url(client_config, "/logs/taylor-order0.log"): (
                    "Publishing report directory /tmp/report to "
                    f"/srv/reports/herbie/{report_name}\n"
                ).encode("utf-8"),
                report_url + "/nightly_info.json": json.dumps(manifest).encode("utf-8"),
                report_url + "/index.html": b"<h1>ok</h1>\n",
                report_url + "/results.json.gz": gzip.compress(b"{\"ok\":true}\n"),
            }
        )
        entry = cli.LogEntry(
            name="2026-04-19-123456-1-herbie-taylor-order0.log",
            url="/logs/taylor-order0.log",
        )

        with (
            self.client_open_patch(opener),
            mock.patch.object(cli, "iter_entries", return_value=iter([entry])),
            mock.patch.object(cli.subprocess, "run", self.fake_curl_run({
                report_url + "/index.html": b"<h1>ok</h1>\n",
                report_url + "/nightly_info.json": json.dumps(manifest).encode("utf-8"),
                report_url + "/results.json.gz": gzip.compress(b"{\"ok\":true}\n"),
            })),
        ):
            rc = cli.cmd_download(
                client_config,
                "herbie",
                cli.RunSelector("taylor-order0", "2026-04-19", "12:34:56"),
            )

        self.assertEqual(rc, 0)
        report_dir = self.tmpdir / report_name
        self.assertEqual((report_dir / "index.html").read_text(), "<h1>ok</h1>\n")
        self.assertEqual(json.loads((report_dir / "nightly_info.json").read_text())["files"][2]["path"], "results.json.gz")
        self.assertEqual((report_dir / "results.json").read_text(), "{\"ok\":true}\n")

    def test_download_report_files_accepts_logical_manifest_paths_for_gzip(self) -> None:
        client_config = self.client_config()
        report_url = self.report_url(client_config, "herbie/1713570001:taylor-order0:feedface")

        with mock.patch.object(cli.subprocess, "run", self.fake_curl_run({
            report_url + "/results.json.gz": gzip.compress(b"{\"ok\":true}\n"),
        })):
            manifest = cli.parse_manifest(json.dumps({"files": [{"path": "results.json", "gzip": True}]}))
            file_count = cli.download_report_files(
                report_url,
                manifest.files,
                self.tmpdir / "downloaded",
                client_config,
            )

        self.assertEqual(file_count, 1)
        self.assertEqual((self.tmpdir / "downloaded" / "results.json").read_text(), "{\"ok\":true}\n")

    def test_cmd_download_reports_curl_failures_cleanly(self) -> None:
        report_name = "1713570002:taylor-order0:badc0de"
        client_config = self.client_config()
        report_url = self.report_url(client_config, "herbie/" + report_name)
        manifest = {"files": [{"path": "results.json", "gzip": False}]}
        entry = cli.LogEntry(
            name="2026-04-19-123456-1-herbie-taylor-order0.log",
            url="/logs/taylor-order0.log",
        )
        opener = FakeOpener(
            {
                self.absolute_url(client_config, entry.url): (
                    "Publishing report directory /tmp/report to "
                    f"/srv/reports/herbie/{report_name}\n"
                ).encode("utf-8"),
                report_url + "/nightly_info.json": json.dumps(manifest).encode("utf-8"),
            }
        )
        error = subprocess.CalledProcessError(22, ["curl", "--fail"])

        with (
            self.client_open_patch(opener),
            mock.patch.object(cli, "load_client_config", return_value=client_config),
            mock.patch.object(cli, "infer_repo", return_value="herbie"),
            mock.patch.object(cli, "iter_entries", return_value=iter([entry])),
            mock.patch.object(cli.subprocess, "run", side_effect=error),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            rc = cli.main(["download", "taylor-order0", "2026-04-19", "12:34:56"])

        self.assertEqual(rc, 1)
        self.assertEqual(stderr.getvalue(), "error: curl failed with exit status 22\n")

    def test_cmd_list_accepts_branch_date_and_time_filters(self) -> None:
        entries = [
            cli.LogEntry(
                name="2026-04-19-123456-1-herbie-main.log",
                url="/logs/main.log",
            ),
            cli.LogEntry(
                name="2026-04-19-123456-1-herbie-taylor-order0.log",
                url="/logs/taylor-old.log",
            ),
            cli.LogEntry(
                name="2026-04-20-090832-1-herbie-taylor-order0.log",
                url="/logs/taylor-new.log",
            ),
        ]

        with (
            mock.patch.object(cli, "iter_entries", return_value=iter(reversed(entries))),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            args = cli.build_parser().parse_args(["list", "taylor-order0", "2026-04-20", "090832"])
            rc = cli.cmd_list(
                self.client_config(),
                "herbie",
                cli.RunSelector(args.branch, args.date, args.time),
            )

        self.assertEqual(rc, 0)
        self.assertEqual(stdout.getvalue(), "2026-04-20 09:08:32 taylor-order0\n")

    def test_cmd_list_branch_lists_all_matching_runs(self) -> None:
        entries = [
            cli.LogEntry(
                name="2026-04-19-123456-1-herbie-taylor-order0.log",
                url="/logs/taylor-old.log",
            ),
            cli.LogEntry(
                name="2026-04-20-090832-1-herbie-taylor-order0.log",
                url="/logs/taylor-new.log",
            ),
        ]

        with (
            mock.patch.object(cli, "iter_entries", return_value=iter(reversed(entries))),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            rc = cli.cmd_list(self.client_config(), "herbie", cli.RunSelector("taylor-order0", None, None))

        self.assertEqual(rc, 0)
        self.assertEqual(
            stdout.getvalue(),
            "2026-04-19 12:34:56 taylor-order0\n"
            "2026-04-20 09:08:32 taylor-order0\n",
        )

    def test_main_log_without_branch_defaults_to_current_branch(self) -> None:
        entries = [
            cli.LogEntry(
                name="2026-04-20-090832-1-herbie-taylor-order0.log",
                url="/logs/taylor-new.log",
            ),
            cli.LogEntry(
                name="2026-04-19-123456-1-herbie-main.log",
                url="/logs/main.log",
            ),
        ]
        opener = FakeOpener({self.absolute_url(self.client_config(), entries[1].url): b"main log\n"})

        with (
            self.client_open_patch(opener),
            mock.patch.object(cli, "load_client_config", return_value=self.client_config()),
            mock.patch.object(cli, "infer_repo", return_value="herbie"),
            mock.patch.object(cli, "current_branch", return_value="main"),
            mock.patch.object(cli, "iter_entries", return_value=iter(entries)),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            rc = cli.main(["log"])

        self.assertEqual(rc, 0)
        self.assertEqual(stdout.getvalue(), "main log\n")

    def test_cmd_status_prints_manifest_metadata(self) -> None:
        report_name = "1713570000:taylor-order0:deadbeef"
        client_config = self.client_config()
        report_url = self.report_url(client_config, "herbie/" + report_name)
        manifest = {
            "repo": "herbie",
            "branch": "taylor-order0",
            "branch_filename": "taylor-order0",
            "commit": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            "commit_short": "deadbeef",
            "status": "success",
            "started_at": "2026-04-20T15:00:00Z",
            "finished_at": "2026-04-20T15:04:30Z",
            "duration_seconds": 270.0,
            "duration_human": "4.5m",
            "log": "taylor-order0.log",
            "log_url": "https://nightly.cs.washington.edu/logs/taylor-order0.log",
            "report_url": report_url,
            "image_url": None,
            "files": [
                {"path": "index.html", "gzip": False},
                {"path": "nightly_info.json", "gzip": False},
            ],
        }
        entry = cli.LogEntry(
            name="2026-04-20-150000-1-herbie-taylor-order0.log",
            url="/logs/taylor-order0.log",
        )
        opener = FakeOpener(
            {
                self.absolute_url(client_config, entry.url): (
                    "Publishing report directory /tmp/report to "
                    f"/srv/reports/herbie/{report_name}\n"
                ).encode("utf-8"),
                report_url + "/nightly_info.json": json.dumps(manifest).encode("utf-8"),
            }
        )

        with (
            self.client_open_patch(opener),
            mock.patch.object(cli, "iter_entries", return_value=iter([entry])),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            args = cli.build_parser().parse_args(["status", "taylor-order0", "2026-04-20", "150000"])
            rc = cli.cmd_status(
                client_config,
                "herbie",
                cli.RunSelector(args.branch, args.date, args.time),
            )

        self.assertEqual(rc, 0)
        self.assertEqual(
            stdout.getvalue(),
            "herbie / taylor-order0\n"
            "\n"
            "Status   success\n"
            "Commit   deadbeef\n"
            "Started  2026-04-20 15:00:00 UTC\n"
            "Finished 2026-04-20 15:04:30 UTC\n"
            "Duration 4.5m\n"
            "Files    2\n"
            f"Report   {report_url}\n"
            "Log      https://nightly.cs.washington.edu/logs/taylor-order0.log\n",
        )

    def test_main_status_without_branch_defaults_to_current_branch(self) -> None:
        report_name = "1713570003:feature:decafbad"
        client_config = self.client_config()
        report_url = self.report_url(client_config, "herbie/" + report_name)
        manifest = {
            "repo": "herbie",
            "branch": "feature",
            "status": "success",
            "files": [],
        }
        entry = cli.LogEntry(
            name="2026-04-21-150000-1-herbie-feature.log",
            url="/logs/feature.log",
        )
        opener = FakeOpener(
            {
                self.absolute_url(client_config, entry.url): (
                    "Publishing report directory /tmp/report to "
                    f"/srv/reports/herbie/{report_name}\n"
                ).encode("utf-8"),
                report_url + "/nightly_info.json": json.dumps(manifest).encode("utf-8"),
            }
        )

        with (
            self.client_open_patch(opener),
            mock.patch.object(cli, "load_client_config", return_value=client_config),
            mock.patch.object(cli, "infer_repo", return_value="herbie"),
            mock.patch.object(cli, "current_branch", return_value="feature"),
            mock.patch.object(cli, "iter_entries", return_value=iter([entry])),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            rc = cli.main(["status"])

        self.assertEqual(rc, 0)
        self.assertIn("herbie / feature\n", stdout.getvalue())

    def test_cmd_status_requires_published_report(self) -> None:
        entry = cli.LogEntry(
            name="2026-04-20-150000-1-herbie-taylor-order0.log",
            url="/logs/taylor-order0.log",
        )
        opener = FakeOpener({self.absolute_url(self.client_config(), entry.url): b"still running\n"})

        with (
            self.client_open_patch(opener),
            mock.patch.object(cli, "iter_entries", return_value=iter([entry])),
        ):
            with self.assertRaisesRegex(cli.CliError, "No published report found in log."):
                args = cli.build_parser().parse_args(["status", "taylor-order0", "2026-04-20", "150000"])
                cli.cmd_status(
                    self.client_config(),
                    "herbie",
                    cli.RunSelector(args.branch, args.date, args.time),
                )


    def test_cmd_setup_saves_client_config(self) -> None:
        state = cli.IndexState(False, [cli.StartTarget("herbie", "main", False)])

        with (
            mock.patch("builtins.input", return_value="alice"),
            mock.patch.object(cli.getpass, "getpass", return_value="secret"),
            mock.patch.object(cli.ClientConfig, "fetch", return_value=""),
            mock.patch.object(cli.IndexParser, "parse", return_value=state),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            rc = cli.cmd_setup("https://nightlies.example")

        self.assertEqual(rc, 0)
        self.assertEqual(stdout.getvalue(), f"Saved CLI config to {cli.client_state_path()}\n")
        self.assertEqual(
            json.loads(cli.client_state_path().read_text()),
            {
                "nightly_url": "https://nightlies.example",
                "username": "alice",
                "password": "secret",
            },
        )
        self.assertEqual(cli.load_client_config(), cli.ClientConfig("https://nightlies.example", "alice", "secret"))

    def test_cmd_setup_refuses_page_without_nightly_controls(self) -> None:
        state = cli.IndexState(False, [])

        with (
            mock.patch("builtins.input", return_value="alice"),
            mock.patch.object(cli.getpass, "getpass", return_value="secret"),
            mock.patch.object(cli.ClientConfig, "fetch", return_value=""),
            mock.patch.object(cli.IndexParser, "parse", return_value=state),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            rc = cli.main(["setup", "https://nightlies.example"])

        self.assertEqual(rc, 1)
        self.assertEqual(stderr.getvalue(), "error: could not find nightly controls at https://nightlies.example\n")
        self.assertFalse(cli.client_state_path().exists())

    def test_main_requires_setup_before_other_commands(self) -> None:
        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            rc = cli.main(["list"])

        self.assertEqual(rc, 1)
        self.assertEqual(stderr.getvalue(), "error: client is not configured. Run `cli setup <url>` to fix.\n")

    def test_main_list_without_branch_does_not_default_to_current_branch(self) -> None:
        entries = [
            cli.LogEntry(
                name="2026-04-19-123456-1-herbie-main.log",
                url="/logs/main.log",
            ),
            cli.LogEntry(
                name="2026-04-20-090832-1-herbie-feature.log",
                url="/logs/feature.log",
            ),
        ]

        with (
            mock.patch.object(cli, "load_client_config", return_value=self.client_config()),
            mock.patch.object(cli, "infer_repo", return_value="herbie"),
            mock.patch.object(cli, "current_branch") as current_branch,
            mock.patch.object(cli, "iter_entries", return_value=iter(reversed(entries))),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            rc = cli.main(["list"])

        self.assertEqual(rc, 0)
        current_branch.assert_not_called()
        self.assertEqual(
            stdout.getvalue(),
            "2026-04-19 12:34:56 main\n"
            "2026-04-20 09:08:32 feature\n",
        )

    def test_nginx_index_parser_uses_relative_log_urls(self) -> None:
        entries = cli.NginxIndexParser.parse(
            '<a href="2026-04-20-090832-1-herbie-main.log">main</a>',
            "/logs/",
        )

        self.assertEqual(
            entries,
            [
                cli.LogEntry(
                    "2026-04-20-090832-1-herbie-main.log",
                    "/logs/2026-04-20-090832-1-herbie-main.log",
                )
            ],
        )

    def test_infer_repo_returns_short_github_repo_name(self) -> None:
        result = subprocess.CompletedProcess(
            ["git", "-C", ".", "remote", "-v"],
            0,
            stdout="origin\tgit@github.com:uwplse/herbie.git (fetch)\n",
        )

        with mock.patch.object(cli.subprocess, "run", return_value=result):
            self.assertEqual(cli.infer_repo("."), "herbie")

    def test_parse_index_state_reads_sync_and_start_controls(self) -> None:
        state = cli.IndexParser.parse(
            """
            <form action="/dryrun" method="post">
              <button disabled>Sync with Github</button>
            </form>
            <form action="runnow" method="post">
              <input type="hidden" name="repo" value="herbie" />
              <input type="hidden" name="branch" value="main" />
              <button>Run</button>
            </form>
            <form action="https://nightly.cs.washington.edu/runnow" method="post">
              <input type="hidden" name="repo" value="ruler" />
              <input type="hidden" name="branch" value="feature/test" />
              <button disabled>Run</button>
            </form>
            """,
            cli.INDEX_PATH,
        )

        self.assertTrue(state.sync_disabled)
        self.assertEqual(
            state.start_targets,
            [
                cli.StartTarget("herbie", "main", False),
                cli.StartTarget("ruler", "feature/test", True),
            ],
        )

    def test_cmd_sync_refuses_when_ui_disables_sync(self) -> None:
        state = cli.IndexState(True, [])

        with (
            mock.patch.object(cli, "load_client_config", return_value=self.client_config()),
            mock.patch.object(cli.ClientConfig, "fetch", return_value=""),
            mock.patch.object(cli.IndexParser, "parse", return_value=state),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            rc = cli.main(["sync"])

        self.assertEqual(rc, 1)
        self.assertEqual(stderr.getvalue(), "error: Nightly sync already running\n")

    def test_cmd_sync_posts_to_dryrun_endpoint(self) -> None:
        requests: list[urllib.request.Request] = []

        class CapturingOpener:
            def open(self, request: object) -> FakeResponse:
                assert not isinstance(request, str)
                requests.append(cast(urllib.request.Request, request))
                return FakeResponse(b"ok")

        with (
            self.client_open_patch(CapturingOpener()),
            mock.patch.object(cli.ClientConfig, "fetch", return_value=""),
            mock.patch.object(cli.IndexParser, "parse", return_value=cli.IndexState(False, [])),
        ):
            rc = cli.cmd_sync(self.client_config())

        self.assertEqual(rc, 0)
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.full_url, urllib.parse.urljoin(self.client_config().index_url, cli.SYNC_PATH))
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.data, b"")

    def test_cmd_start_posts_server_repo_token(self) -> None:
        requests: list[urllib.request.Request] = []

        class CapturingOpener:
            def open(self, request: object) -> FakeResponse:
                assert not isinstance(request, str)
                requests.append(cast(urllib.request.Request, request))
                return FakeResponse(b"ok")

        state = cli.IndexState(False, [cli.StartTarget("herbie", "feature/test", False)])

        with (
            self.client_open_patch(CapturingOpener()),
            mock.patch.object(cli.ClientConfig, "fetch", return_value=""),
            mock.patch.object(cli.IndexParser, "parse", return_value=state),
        ):
            rc = cli.cmd_start(self.client_config(), "herbie", "feature/test")

        self.assertEqual(rc, 0)
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.full_url, urllib.parse.urljoin(self.client_config().index_url, cli.START_PATH))
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.data, b"repo=herbie&branch=feature%2Ftest")

    def test_cmd_start_refuses_queued_branch(self) -> None:
        state = cli.IndexState(False, [cli.StartTarget("herbie", "feature/test", True)])

        with (
            mock.patch.object(cli, "load_client_config", return_value=self.client_config()),
            mock.patch.object(cli, "infer_repo", return_value="herbie"),
            mock.patch.object(cli.ClientConfig, "fetch", return_value=""),
            mock.patch.object(cli.IndexParser, "parse", return_value=state),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            rc = cli.main(["start", "feature/test"])

        self.assertEqual(rc, 1)
        self.assertEqual(
            stderr.getvalue(),
            "error: Branch feature/test on herbie already queued\n",
        )

    def test_cmd_start_explains_that_sync_may_add_a_missing_branch(self) -> None:
        state = cli.IndexState(False, [cli.StartTarget("herbie", "main", False)])

        with (
            mock.patch.object(cli, "load_client_config", return_value=self.client_config()),
            mock.patch.object(cli, "infer_repo", return_value="herbie"),
            mock.patch.object(cli.ClientConfig, "fetch", return_value=""),
            mock.patch.object(cli.IndexParser, "parse", return_value=state),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            rc = cli.main(["start", "feature/test"])

        self.assertEqual(rc, 1)
        self.assertEqual(
            stderr.getvalue(),
            "error: branch 'feature/test' is not available for repo 'herbie'; "
            "if you just pushed it to GitHub, run `nightlies sync` and try again\n",
        )

    def test_main_start_without_branch_defaults_to_current_branch(self) -> None:
        requests: list[urllib.request.Request] = []

        class CapturingOpener:
            def open(self, request: object) -> FakeResponse:
                assert not isinstance(request, str)
                requests.append(cast(urllib.request.Request, request))
                return FakeResponse(b"ok")

        state = cli.IndexState(False, [cli.StartTarget("herbie", "feature/test", False)])

        with (
            self.client_open_patch(CapturingOpener()),
            mock.patch.object(cli, "load_client_config", return_value=self.client_config()),
            mock.patch.object(cli, "infer_repo", return_value="herbie"),
            mock.patch.object(cli, "current_branch", return_value="feature/test"),
            mock.patch.object(cli.ClientConfig, "fetch", return_value=""),
            mock.patch.object(cli.IndexParser, "parse", return_value=state),
        ):
            rc = cli.main(["start"])

        self.assertEqual(rc, 0)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].data, b"repo=herbie&branch=feature%2Ftest")

    def test_cmd_start_surfaces_http_error_message(self) -> None:
        client_config = self.client_config()

        class ErrorOpener:
            def open(self, request: object) -> FakeResponse:
                assert not isinstance(request, str)
                raise urllib.error.HTTPError(
                    urllib.parse.urljoin(client_config.index_url, cli.START_PATH),
                    409,
                    "Conflict",
                    hdrs=None,
                    fp=io.BytesIO(b"Nightly sync already running"),
                )

        state = cli.IndexState(False, [cli.StartTarget("herbie", "main", False)])

        with (
            self.client_open_patch(ErrorOpener()),
            mock.patch.object(cli, "load_client_config", return_value=client_config),
            mock.patch.object(cli, "infer_repo", return_value="herbie"),
            mock.patch.object(cli.ClientConfig, "fetch", return_value=""),
            mock.patch.object(cli.IndexParser, "parse", return_value=state),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            rc = cli.main(["start", "main"])

        self.assertEqual(rc, 1)
        self.assertEqual(stderr.getvalue(), "error: Nightly sync already running\n")


class TestNightlyRunnerHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="nr-harness-"))
        self.repos_dir = self.tmpdir / "repos"
        self.logs_dir = self.tmpdir / "logs"
        self.reports_dir = self.tmpdir / "reports"
        self.work_dir = self.tmpdir / "work"
        self.remote_dir = self.tmpdir / "origin.git"
        self.subrepo_dir = self.tmpdir / "subrepo"
        self.config_file = self.tmpdir / "nightlies.conf"
        self.pid_file = self.tmpdir / "running.pid"
        self.old_git_allow_protocol = os.environ.get("GIT_ALLOW_PROTOCOL")
        os.environ["GIT_ALLOW_PROTOCOL"] = "file"

        self.repos_dir.mkdir()
        self.logs_dir.mkdir()
        self.reports_dir.mkdir()

        self.git(["init", "--initial-branch=main", str(self.work_dir)], repo=self.tmpdir)
        self.git(["config", "user.name", "NR Harness"], repo=self.work_dir)
        self.git(["config", "user.email", "nr-harness@example.com"], repo=self.work_dir)
        self.commit(self.work_dir, "README.md", "seed\n", "initial main commit")

        self.git(["init", "--bare", "--initial-branch=main", str(self.remote_dir)], repo=self.tmpdir)
        self.git(["remote", "add", "origin", str(self.remote_dir)], repo=self.work_dir)
        self.git(["push", "origin", "main"], repo=self.work_dir)

        self.git(["init", "--initial-branch=main", str(self.subrepo_dir)], repo=self.tmpdir)
        self.git(["config", "user.name", "NR Harness"], repo=self.subrepo_dir)
        self.git(["config", "user.email", "nr-harness@example.com"], repo=self.subrepo_dir)
        self.commit(self.subrepo_dir, "sub.txt", "i am a submodule\n", "initial submodule commit")

    def tearDown(self) -> None:
        if self.old_git_allow_protocol is None:
            os.environ.pop("GIT_ALLOW_PROTOCOL", None)
        else:
            os.environ["GIT_ALLOW_PROTOCOL"] = self.old_git_allow_protocol
        shutil.rmtree(self.tmpdir)

    def with_cwd(self, path: Path) -> tuple[str, Path]:
        old = os.getcwd()
        os.chdir(path)
        return old, path

    def test_nr_dryrun_vanilla(self) -> None:
        first = self.nightly(repo_updates={}, complete=True)
        self.assertEqual(first, ["main"])

        second = self.nightly()
        self.assertEqual(second, [])

        self.create_branch("feature/test", "feature.txt", "v1\n", "initial feature commit")

        third = self.nightly()
        self.assertEqual(third, ["feature/test"])

    def test_nr_dryrun_baseline(self) -> None:
        first = self.nightly(repo_updates={"baseline": "main"}, complete=True)
        self.assertEqual(first, ["main"])

        second = self.nightly()
        self.assertEqual(second, [])

        self.create_branch("feature/test", "feature.txt", "v1\n", "initial feature commit")

        third = self.nightly()
        self.assertEqual(third, ["main", "feature/test"])

    def test_brand_new_repo_creates_checkout_and_worktrees(self) -> None:
        ran = self.nightly(repo_updates={})
        self.assertEqual(ran, ["main"])

        repo_dir = self.repos_dir / "testrepo"
        self.assertTrue((repo_dir / ".checkout").is_dir())
        self.assertTrue((repo_dir / "main").is_dir())
        self.assertFalse((repo_dir / "feature_2ftest").exists())

    def test_adding_new_branch_gets_scheduled(self) -> None:
        first = self.nightly(repo_updates={}, complete=True)
        self.assertEqual(first, ["main"])

        self.create_branch("hotfix/new", "hotfix.txt", "v1\n", "add hotfix branch")

        ran = self.nightly()
        self.assertEqual(ran, ["hotfix/new"])

    def test_deleting_branch_removes_worktree_directory(self) -> None:
        self.create_branch("feature/test", "feature.txt", "v1\n", "initial feature commit")
        self.nightly(repo_updates={}, complete=True)

        repo_dir = self.repos_dir / "testrepo"
        deleted_branch_dir = repo_dir / "feature_2ftest"
        self.assertTrue(deleted_branch_dir.is_dir())

        self.git(["branch", "-D", "feature/test"], repo=self.remote_dir)
        ran = self.nightly()

        self.assertEqual(ran, [])
        self.assertFalse(deleted_branch_dir.exists())

    def test_commits_only_schedule_changed_branches(self) -> None:
        self.create_branch("feature/test", "feature.txt", "v1\n", "initial feature commit")
        self.nightly(repo_updates={}, complete=True)

        self.git(["checkout", "main"], repo=self.work_dir)
        self.commit(self.work_dir, "README.md", "seed\nmain v2\n", "update main")
        self.git(["push", "origin", "main"], repo=self.work_dir)

        ran_main = self.nightly()
        self.assertEqual(ran_main, ["main"])
        self.nightly(complete=True)

        self.git(["checkout", "feature/test"], repo=self.work_dir)
        self.commit(self.work_dir, "feature.txt", "v4\n", "update feature")
        self.git(["push", "origin", "feature/test"], repo=self.work_dir)
        self.git(["checkout", "main"], repo=self.work_dir)

        ran_feature = self.nightly()
        self.assertEqual(ran_feature, ["feature/test"])

    def test_runner_publishes_reports(self) -> None:
        self.makefile(
            "add report-producing nightly target",
            ["printf '<h1>ok</h1>\\n' > report/index.html"],
        )
        self.git(["push", "origin", "main"], repo=self.work_dir)

        self.nightly(
            repo_updates={"branches": "main", "report": "report"},
            complete=True,
        )
        result = self.run_runner("main", "publish-report.log")
        report_dir = self.published_report(result)
        published = report_dir / "index.html"
        self.assertTrue(published.exists())
        self.assertEqual(published.read_text(), "<h1>ok</h1>\n")
        info = json.loads((report_dir / "nightly_info.json").read_text())
        self.assertEqual(info["repo"], "testrepo")
        self.assertEqual(info["branch"], "main")
        self.assertEqual(info["branch_filename"], "main")
        self.assertEqual(info["status"], "success")
        self.assertRegex(info["commit"], r"^[0-9a-f]{40}$")
        self.assertEqual(info["commit_short"], info["commit"][:8])
        self.assertEqual(info["log"], "publish-report.log")
        self.assertEqual(
            info["report_url"],
            "https://nightlies.example/reports/testrepo/" + report_dir.name,
        )
        self.assertEqual(
            info["log_url"],
            "https://nightlies.example/logs/publish-report.log",
        )
        self.assertIsNone(info["image_url"])
        self.assertEqual(
            info["files"],
            [
                {"path": "index.html", "gzip": False},
                {"path": "nightly_info.json", "gzip": False},
            ],
        )
        # Published reports should be moved out of the worktree so later runs start clean.
        self.assertFalse((self.repos_dir / "testrepo" / "main" / "report").exists())

    def test_runner_records_gzipped_files_in_nightly_info(self) -> None:
        self.makefile(
            "add gzipped report-producing nightly target",
            [
                "printf '<h1>ok</h1>\\n' > report/index.html",
                "printf '{\"ok\":true}\\n' > report/results.json",
            ],
        )
        self.git(["push", "origin", "main"], repo=self.work_dir)

        self.nightly(
            repo_updates={"branches": "main", "report": "report", "gzip": "*.json"},
            complete=True,
        )
        result = self.run_runner("main", "publish-gzip-report.log")
        report_dir = self.published_report(result)
        info = json.loads((report_dir / "nightly_info.json").read_text())

        self.assertEqual(
            info["files"],
            [
                {"path": "index.html", "gzip": False},
                {"path": "nightly_info.json", "gzip": False},
                {"path": "results.json.gz", "gzip": True},
            ],
        )

    def test_runner_handles_invalid_report_destination(self) -> None:
        invalid_reports = self.tmpdir / "reports-file"
        invalid_reports.write_text("not a directory\n")

        self.makefile(
            "add nightly report for invalid destination case",
            ["printf 'broken publish path\\n' > report/index.txt"],
        )
        self.git(["push", "origin", "main"], repo=self.work_dir)

        self.nightly(
            repo_updates={"branches": "main", "report": "report"},
            default_updates={"reports": str(invalid_reports)},
            complete=True,
        )
        result = self.run_runner("main", "invalid-report.log")
        self.assertIn("Error saving report for `main`", result.stdout)
        self.assertTrue((self.repos_dir / "testrepo" / "main" / "report" / "index.txt").exists())

    def test_runner_publishes_nested_report_directory(self) -> None:
        self.makefile(
            "add nested report-producing nightly target",
            [
                "mkdir -p nested/out/report",
                "printf '<h1>nested</h1>\\n' > nested/out/report/index.html",
            ],
        )
        self.git(["push", "origin", "main"], repo=self.work_dir)

        self.nightly(
            repo_updates={"branches": "main", "report": "nested/out/report"},
            complete=True,
        )
        result = self.run_runner("main", "publish-nested-report.log")
        report_dir = self.published_report(result)
        published = report_dir / "index.html"
        self.assertTrue(published.exists())
        self.assertEqual(published.read_text(), "<h1>nested</h1>\n")

    def test_runner_tracks_remote_feature_branch_tip(self) -> None:
        self.create_branch(
            "feature/test",
            "Makefile",
            self.makefile_text(["printf '<h1>feature-v1</h1>\\n' > report/index.html"]),
            "feature v1",
        )

        self.nightly(
            repo_updates={"branches": "feature/test", "report": "report"},
            complete=True,
        )
        first = self.run_runner("feature/test", "feature-tip-v1.log")
        first_dir = self.published_report(first)
        self.assertEqual((first_dir / "index.html").read_text(), "<h1>feature-v1</h1>\n")

        self.git(["checkout", "feature/test"], repo=self.work_dir)
        self.commit(
            self.work_dir,
            "Makefile",
            self.makefile_text(["printf '<h1>feature-v2</h1>\\n' > report/index.html"]),
            "feature v2",
        )
        self.git(["push", "origin", "feature/test"], repo=self.work_dir)
        self.git(["checkout", "main"], repo=self.work_dir)

        # Refresh origin refs in the branch worktree before rerunning branch runner.
        self.nightly(repo_updates={"branches": "feature/test", "report": "report"})
        second = self.run_runner("feature/test", "feature-tip-v2.log")
        second_dir = self.published_report(second)
        self.assertEqual((second_dir / "index.html").read_text(), "<h1>feature-v2</h1>\n")

    def test_submodule_regression(self) -> None:
        # Initial NR checkout/worktree setup without submodules.
        self.nightly(repo_updates={"branches": "main"}, complete=True)

        self.git(["checkout", "main"], repo=self.work_dir)
        (self.work_dir / "Makefile").write_text(self.makefile_text(["echo nightly-ok"]))
        self.git(["add", "Makefile"], repo=self.work_dir)
        self.git(
            [
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                str(self.subrepo_dir),
                "sub1",
            ],
            repo=self.work_dir,
        )
        self.git(["commit", "-m", "add local submodule"], repo=self.work_dir)
        self.git(["push", "origin", "main"], repo=self.work_dir)

        # This mirrors the repro's post-submodule-add fetch path.
        self.assertEqual(self.nightly(), ["main"])

        # This mirrors the repro's reset+submodule-update in a worktree path.
        self.run_runner("main", "submodule-regression.log")

    def test_nightly_clears_transient_runner_state(self) -> None:
        self.write_config(repo_updates={})
        runner = NightlyRunner(str(self.config_file))
        runner.load()
        old_cwd, _ = self.with_cwd(self.tmpdir)
        try:
            runner.run()
        finally:
            os.chdir(old_cwd)
        self.assertNotIn("repo", runner.data)
        self.assertIn("pid", runner.data)
        self.assertIn("log", runner.data)

    def test_nightly_writes_log_under_configured_logs_dir(self) -> None:
        self.nightly(repo_updates={})
        logs = sorted(self.logs_dir.glob("*.log"))
        self.assertGreaterEqual(len(logs), 1)
        self.assertTrue(all(p.parent == self.logs_dir for p in logs))

    def test_clean_removes_unknown_files_but_keeps_branch_metadata(self) -> None:
        self.create_branch("feature/test", "feature.txt", "v1\n", "initial feature commit")
        self.nightly(repo_updates={}, complete=True)
        repo_dir = self.repos_dir / "testrepo"
        junk_dir = repo_dir / "junk-dir"
        junk_file = repo_dir / "junk.txt"
        junk_dir.mkdir()
        junk_file.write_text("junk\n")

        self.nightly(repo_updates={})

        self.assertFalse(junk_dir.exists())
        # Dry-run keeps unknown files but should still remove unknown directories.
        self.assertTrue(junk_file.exists())
        self.assertTrue((repo_dir / "main.json").exists())
        self.assertTrue((repo_dir / "feature_2ftest.json").exists())

    def test_runner_timeout_returns_failure_and_writes_metadata(self) -> None:
        self.makefile(
            "add timeout nightly target",
            [
                "sleep 1",
                "printf '<h1>late</h1>\\n' > report/index.html",
            ],
        )
        self.git(["push", "origin", "main"], repo=self.work_dir)

        self.nightly(
            repo_updates={"branches": "main", "report": "report", "timeout": "0.1s"},
            complete=True,
        )
        result = self.run_runner("main", "timeout.log", expected_returncode=1)
        self.assertIn("timed out", result.stdout.lower())
        metadata = self.repos_dir / "testrepo" / "main.json"
        self.assertTrue(metadata.exists())
        contents = metadata.read_text()
        self.assertIn('"commit"', contents)
        self.assertIn('"time"', contents)
        report_dir = self.published_report(result)
        self.assertFalse((report_dir / "nightly_info.json").exists())

    def test_runner_writes_metadata_for_successful_run(self) -> None:
        self.makefile("add simple nightly target", ["echo ok"])
        self.git(["push", "origin", "main"], repo=self.work_dir)

        self.nightly(repo_updates={"branches": "main"}, complete=True)
        self.run_runner("main", "metadata-success.log")
        metadata = self.repos_dir / "testrepo" / "main.json"
        self.assertTrue(metadata.exists())
        contents = metadata.read_text()
        self.assertIn('"commit"', contents)
        self.assertIn('"time"', contents)

    def test_dryrun_rejects_when_sync_is_running(self) -> None:
        self.write_config(repo_updates={})
        self.pid_file.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "start": "2026-04-21T15:00:00",
                    "config": str(self.config_file),
                    "log": str(self.logs_dir / "running.log"),
                }
            )
        )

        bottle = types.ModuleType("bottle")

        class HTTPError(Exception):
            def __init__(self, status_code: int, body: str = "") -> None:
                super().__init__(body)
                self.status_code = status_code
                self.body = body

        def decorator(*_args: object, **_kwargs: object) -> object:
            return lambda fn: fn

        bottle.HTTPError = HTTPError
        bottle.route = decorator
        bottle.post = decorator
        bottle.view = decorator
        bottle.static_file = lambda *_args, **_kwargs: None
        bottle.redirect = lambda _url: None
        bottle.run = lambda *_args, **_kwargs: None

        status = types.ModuleType("status")
        status.system_state_html = lambda: ""

        with mock.patch.dict(sys.modules, {"bottle": bottle, "status": status}):
            sys.modules.pop("server", None)
            server = importlib.import_module("server")
            try:
                with (
                    mock.patch.object(server, "CONF_FILE", str(self.config_file)),
                    mock.patch.object(server, "run_nightlies") as run_nightlies,
                    mock.patch.object(bottle, "redirect") as redirect,
                ):
                    with self.assertRaises(HTTPError) as ctx:
                        server.dryrun()
            finally:
                sys.modules.pop("server", None)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.body, "Nightly sync already running")
        run_nightlies.assert_not_called()
        redirect.assert_not_called()

    def test_load_normalizes_baseurl_with_trailing_slash(self) -> None:
        self.write_config(repo_updates={}, default_updates={"baseurl": "https://nightlies.example"})
        runner = NightlyRunner(str(self.config_file))
        runner.load()
        self.assertEqual(runner.base_url, "https://nightlies.example/")

    def write_config(
        self,
        repo_updates: dict[str, str],
        default_updates: dict[str, str] | None = None,
    ) -> None:
        conf = configparser.ConfigParser()
        defaults = {
            "repos": str(self.repos_dir),
            "logs": str(self.logs_dir),
            "reports": str(self.reports_dir),
            "baseurl": "https://nightlies.example/",
            "pid": str(self.pid_file),
            "dryrun": "1",
        }
        if default_updates:
            defaults.update(default_updates)
        conf["DEFAULT"] = defaults
        repo_config = {"url": str(self.remote_dir)}
        repo_config.update(repo_updates)
        conf["testrepo"] = repo_config
        with self.config_file.open("w") as f:
            conf.write(f)

    def nightly(
        self,
        repo_updates: dict[str, str] | None = None,
        default_updates: dict[str, str] | None = None,
        complete: bool = False,
    ) -> list[str]:
        if repo_updates is not None or default_updates is not None or not self.config_file.exists():
            self.write_config(repo_updates or {}, default_updates)

        runner = NightlyRunner(str(self.config_file))
        runner.load()
        old_cwd, _ = self.with_cwd(self.tmpdir)
        try:
            runner.run()
        finally:
            os.chdir(old_cwd)
        repo = runner.repos[0]
        ran = [branch.name for branch in repo.runnable]
        if complete:
            for branch in repo.runnable:
                branch.config["commit"] = branch.current_commit
                branch.save_metadata()
        return ran

    def run_runner(
        self,
        branch: str,
        log_name: str,
        expected_returncode: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["python3", "runner.py", str(self.config_file), "testrepo", branch, log_name],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, expected_returncode, msg=result.stdout + "\n" + result.stderr)
        return result

    def published_report(self, result: subprocess.CompletedProcess[str]) -> Path:
        marker = "Publishing report directory "
        for line in result.stdout.splitlines():
            if marker in line and " to " in line:
                return Path(line.split(" to ", 1)[1].strip())
        self.fail("Could not find published report directory in runner output")

    def create_branch(self, branch: str, filename: str, contents: str, message: str) -> None:
        self.git(["checkout", "-b", branch], repo=self.work_dir)
        self.commit(self.work_dir, filename, contents, message)
        self.git(["push", "origin", branch], repo=self.work_dir)
        self.git(["checkout", "main"], repo=self.work_dir)

    def commit(self, repo: Path, filename: str, contents: str, message: str) -> None:
        file = repo / filename
        file.write_text(contents)
        self.git(["add", filename], repo=repo)
        self.git(["commit", "-m", message], repo=repo)

    def makefile_text(self, commands: list[str], target: str = "nightly") -> str:
        lines = [f"{target}:\n"]
        for command in commands:
            cmd = command.lstrip()
            if not cmd.startswith("@"):
                cmd = "@" + cmd
            lines.append(f"\t{cmd}\n")
        return "".join(lines)

    def makefile(
        self,
        message: str,
        commands: list[str],
        repo: Path | None = None,
        target: str = "nightly",
    ) -> None:
        self.commit(repo or self.work_dir, "Makefile", self.makefile_text(commands, target), message)

    def git(self, cmd: list[str], repo: Path) -> None:
        full_cmd = ["git", "-C", str(repo)] + cmd
        result = subprocess.run(full_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"git command failed: {' '.join(full_cmd)}\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}"
            )


class TestServerRunNow(unittest.TestCase):
    def import_server(self) -> Any:
        class FakeHTTPError(Exception):
            def __init__(self, status_code: int, body: str) -> None:
                super().__init__(body)
                self.status_code = status_code
                self.body = body

        class FakeBottleModule:
            def __init__(self) -> None:
                self.request = SimpleNamespace(forms={})
                self.HTTPError = FakeHTTPError

            def route(self, *_args: Any, **_kwargs: Any) -> Any:
                return lambda fn: fn

            def post(self, *_args: Any, **_kwargs: Any) -> Any:
                return lambda fn: fn

            def view(self, *_args: Any, **_kwargs: Any) -> Any:
                return lambda fn: fn

            def redirect(self, *_args: Any, **_kwargs: Any) -> None:
                raise AssertionError("redirect should not be reached in error-path tests")

            def static_file(self, *_args: Any, **_kwargs: Any) -> None:
                raise AssertionError("static_file should not be reached in runnow tests")

        sys.modules.pop("server", None)
        fake_bottle = FakeBottleModule()
        fake_status = SimpleNamespace(system_state_html=lambda: "")
        with mock.patch.dict(sys.modules, {"bottle": fake_bottle, "status": fake_status}):
            return importlib.import_module("server")

    def make_runner(self, repo: str, branches: dict[str, Any]) -> tuple[Any, Any]:
        repo_state = SimpleNamespace(
            name=repo,
            branches=branches,
            read=mock.Mock(),
        )
        runner = SimpleNamespace(
            data=None,
            repos=[repo_state],
            load=mock.Mock(),
            load_pid=mock.Mock(),
            config=configparser.ConfigParser(),
        )
        runner.config["testrepo"] = {}
        return runner, repo_state

    def test_runnow_rejects_branch_not_available_on_nightly(self) -> None:
        server = self.import_server()
        runner, repo_state = self.make_runner("testrepo", {})

        with (
            mock.patch.object(server.nightlies, "NightlyRunner", return_value=runner),
            mock.patch.object(server.bottle, "request", SimpleNamespace(forms={"repo": "testrepo", "branch": "feature/test"})),
            mock.patch.object(server, "run_nightlies") as run_nightlies,
        ):
            with self.assertRaises(server.bottle.HTTPError) as ctx:
                server.runnow()

        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.body, "Branch feature/test is not available on nightly testrepo")
        repo_state.read.assert_called_once_with()
        run_nightlies.assert_not_called()

    def test_runnow_reports_queued_branch_with_job_name(self) -> None:
        server = self.import_server()
        branch = SimpleNamespace(
            badges=["queued"],
            filename="feature_2ftest",
        )
        runner, repo_state = self.make_runner("testrepo", {"feature/test": branch})

        with (
            mock.patch.object(server.nightlies, "NightlyRunner", return_value=runner),
            mock.patch.object(server.bottle, "request", SimpleNamespace(forms={"repo": "testrepo", "branch": "feature/test"})),
            mock.patch.object(server, "run_nightlies") as run_nightlies,
        ):
            with self.assertRaises(server.bottle.HTTPError) as ctx:
                server.runnow()

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.body, "Job nightly:testrepo:feature_2ftest already queued")
        repo_state.read.assert_called_once_with()
        run_nightlies.assert_not_called()


if __name__ == "__main__":
    unittest.main()
