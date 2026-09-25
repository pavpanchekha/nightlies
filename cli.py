#!/usr/bin/env python3

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
import argparse
import datetime
import getpass
import gzip
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser


## Setup & config file

STATE_FILENAME = "state.json"
API_PATH = "/api"
API_LOGS_PATH = "/api/logs"
LOG_PAGE_SIZE = 10
REPORTS_PATH = "/reports/"
SYNC_PATH = "/dryrun"
START_PATH = "/runnow"
SYNC_POLL_INTERVAL = 1.0
SYNC_START_TIMEOUT = 10.0
SYNC_FINISH_TIMEOUT = 30 * 60.0


class CliError(Exception):
    pass


class MissingClientConfig(CliError):
    pass


class InvalidClientConfig(CliError):
    pass


@dataclass(frozen=True)
class ClientConfig:
    index_url: str
    username: str
    password: str

    def open(self, request: str | urllib.request.Request) -> urllib.response.addinfourl:
        password_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        password_mgr.add_password(None, self.index_url, self.username, self.password)
        opener = urllib.request.build_opener(urllib.request.HTTPBasicAuthHandler(password_mgr))
        return opener.open(request)

    def fetch(self, url: str) -> str:
        with self.open(urllib.parse.urljoin(self.index_url, url)) as response:
            return response.read().decode("utf-8", errors="replace")

    def fetch_json(self, url: str) -> dict[str, object]:
        try:
            payload = json.loads(self.fetch(url))
        except json.JSONDecodeError as exc:
            raise CliError(f"invalid JSON response from {url}: {exc}") from exc
        if not isinstance(payload, dict):
            raise CliError(f"invalid JSON response from {url}: expected an object")
        return payload

    def post(self, url: str, fields: dict[str, str]) -> None:
        request = urllib.request.Request(
            urllib.parse.urljoin(self.index_url, url),
            data=urllib.parse.urlencode(fields).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with self.open(request) as response:
            response.read()


def client_state_path() -> Path:
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "nightlies" / STATE_FILENAME
        return Path.home() / "AppData" / "Roaming" / "nightlies" / STATE_FILENAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "nightlies" / STATE_FILENAME
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home) / "nightlies" / STATE_FILENAME
    return Path.home() / ".local" / "share" / "nightlies" / STATE_FILENAME


def save_client_config(client_config: ClientConfig) -> Path:
    path = client_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path.parent, 0o700)

    with path.open("w", encoding="utf-8") as handle:
        json.dump(
            {
                "nightly_url": client_config.index_url,
                "username": client_config.username,
                "password": client_config.password,
            },
            handle,
            indent=2,
        )
        handle.write("\n")
    if os.name != "nt":
        os.chmod(path, 0o600)
    return path


def load_client_config() -> ClientConfig:
    nightly_url = os.environ.get("NIGHTLIES_URL")
    username = os.environ.get("NIGHTLIES_USERNAME")
    password = os.environ.get("NIGHTLIES_PASSWORD")
    if any(value is not None for value in (nightly_url, username, password)):
        if not nightly_url or not username or not password:
            raise InvalidClientConfig(
                "environment configuration requires NIGHTLIES_URL, "
                "NIGHTLIES_USERNAME, and NIGHTLIES_PASSWORD"
            )
        return ClientConfig(nightly_url, username, password)

    path = client_state_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MissingClientConfig("client is not configured") from exc
    except json.JSONDecodeError as exc:
        raise InvalidClientConfig(f"invalid client config {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise InvalidClientConfig(f"invalid client config {path}: expected a JSON object")

    nightly_url = payload.get("nightly_url")
    username = payload.get("username")
    password = payload.get("password")
    if not isinstance(nightly_url, str) or not isinstance(username, str) or not isinstance(password, str):
        raise InvalidClientConfig(
            f"invalid client config {path}: expected string fields nightly_url, username, and password"
        )
    return ClientConfig(nightly_url, username, password)


## Server API

@dataclass(frozen=True)
class StartTarget:
    repo: str
    branch: str
    disabled: bool


@dataclass(frozen=True)
class IndexState:
    sync_disabled: bool
    start_targets: list[StartTarget]
    

def parse_control_state(payload: dict[str, object]) -> IndexState:
    sync_disabled = payload.get("sync_disabled")
    targets = payload.get("start_targets")
    if not isinstance(sync_disabled, bool) or not isinstance(targets, list):
        raise CliError("invalid control response")
    start_targets: list[StartTarget] = []
    for target in targets:
        if not isinstance(target, dict):
            raise CliError("invalid control response")
        repo = target.get("repo")
        branch = target.get("branch")
        disabled = target.get("disabled")
        if not isinstance(repo, str) or not isinstance(branch, str) or not isinstance(disabled, bool):
            raise CliError("invalid control response")
        start_targets.append(StartTarget(repo, branch, disabled))
    return IndexState(sync_disabled, start_targets)


def parse_log_entries(payload: dict[str, object]) -> list[str]:
    logs = payload.get("logs")
    if not isinstance(logs, list):
        raise CliError("invalid log response")
    entries: list[str] = []
    for log in logs:
        if not isinstance(log, dict):
            raise CliError("invalid log response")
        name = log.get("name")
        if not isinstance(name, str):
            raise CliError("invalid log response")
        entries.append(name)
    return entries


@dataclass(frozen=True)
class RunLog:
    name: str
    date: str
    time: str
    branch: str


@dataclass(frozen=True)
class RunSelector:
    branch: str | None
    date: str | None
    time: str | None


@dataclass(frozen=True)
class ManifestFile:
    remote_path: str
    local_path: str


@dataclass(frozen=True)
class Manifest:
    repo: str | None
    branch: str | None
    status: str | None
    commit: str | None
    started_at: str | None
    finished_at: str | None
    duration: str | float | int | None
    report_url: str | None
    log_url: str | None
    image_url: str | None
    files: list[ManifestFile]

    def text(self) -> str:
        lines: list[str] = []
        if self.repo is not None and self.branch is not None:
            lines.append(f"{self.repo} / {self.branch}")
            lines.append("")

        details: list[tuple[str, object]] = []
        if self.status is not None:
            details.append(("Status", self.status))
        if self.commit is not None:
            details.append(("Commit", self.commit))
        if self.started_at is not None:
            details.append(("Started", self.started_at))
        if self.finished_at is not None:
            details.append(("Finished", self.finished_at))
        if self.duration is not None:
            details.append(("Duration", self.duration))
        details.append(("Files", len(self.files)))
        if self.report_url is not None:
            details.append(("Report", self.report_url))
        if self.log_url is not None:
            details.append(("Log", self.log_url))
        if self.image_url is not None:
            details.append(("Image", self.image_url))

        for label, value in details:
            lines.append(f"{label:8} {value}")
        return "\n".join(lines)

## Log index

def iter_entries(
    client_config: ClientConfig,
    repo: str | None = None,
    selector: RunSelector | None = None,
) -> Iterator[str]:
    params: dict[str, str] = {}
    if repo is not None:
        params["repo"] = repo
    if selector is not None:
        if selector.branch is not None:
            params["branch"] = selector.branch
        if selector.date is not None:
            params["date"] = selector.date
        if selector.time is not None:
            params["time"] = selector.time
    params["limit"] = str(LOG_PAGE_SIZE)
    query = urllib.parse.urlencode(params)
    url = API_LOGS_PATH + ("?" + query if query else "")
    yield from parse_log_entries(client_config.fetch_json(url))


## Repo discovery

def github_repo_name(url: str) -> str | None:
    if url.startswith("git@github.com:"):
        path = url.removeprefix("git@github.com:")
    elif url.startswith("https://github.com/"):
        path = url.removeprefix("https://github.com/")
    else:
        return None
    path = path.removesuffix(".git")
    parts = path.split("/")
    if len(parts) != 2 or not all(parts):
        return None
    return parts[1]


def infer_repo(cwd: str) -> str:
    result = subprocess.run(
        ["git", "-C", cwd, "remote", "-v"],
        capture_output=True,
        text=True,
        check=True,
    )
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            repo = github_repo_name(parts[1])
            if repo is not None:
                return repo
    raise CliError(f"could not infer GitHub repo from git remotes in {cwd}")


def current_branch(cwd: str) -> str:
    result = subprocess.run(
        ["git", "-C", cwd, "branch", "--show-current"],
        capture_output=True,
        text=True,
        check=True,
    )
    branch = result.stdout.strip()
    if not branch:
        raise CliError(f"could not infer current Git branch in {cwd}")
    return branch


## Run logs

def parse_run_log(repo: str, name: str) -> RunLog | None:
    parts = Path(name).stem.split("-")
    if len(parts) < 5:
        return None
    if len(parts[0]) != 4 or len(parts[1]) != 2 or len(parts[2]) != 2:
        return None
    raw_time = parts[3]
    if len(raw_time) != 6 or not raw_time.isdigit():
        return None

    rest = parts[4:]
    if rest and rest[0].isdigit():
        rest = rest[1:]
    repo_parts = repo.split("-")
    if rest[: len(repo_parts)] != repo_parts:
        return None
    branch_parts = rest[len(repo_parts) :]
    if not branch_parts:
        return None
    return RunLog(
        name=name,
        date="-".join(parts[:3]),
        time=":".join([raw_time[:2], raw_time[2:4], raw_time[4:6]]),
        branch="-".join(branch_parts),
    )


def matching_run_logs(
    entries: Iterable[str],
    repo: str,
    selector: RunSelector,
) -> Iterator[RunLog]:
    for name in entries:
        run = parse_run_log(repo, name)
        if run is None:
            continue
        if selector.branch is not None and run.branch != selector.branch:
            continue
        if selector.date is not None and not run.date.startswith(selector.date):
            continue
        if selector.time is not None and run.time != selector.time:
            continue
        yield run


def log_url(client_config: ClientConfig, name: str) -> str:
    return urllib.parse.urljoin(
        client_config.index_url,
        "logs/" + urllib.parse.quote(name),
    )


## Server controls

def resolve_start_target(
    index_state: IndexState,
    repo: str,
    branch: str,
) -> StartTarget:
    for target in index_state.start_targets:
        if target.branch == branch and target.repo == repo:
            return target

    if repo in {target.repo for target in index_state.start_targets}:
        raise CliError(
            f"branch {branch!r} is not available for repo {repo!r}; "
            "if you just pushed it to GitHub, run `nightlies sync` and try again"
        )
    raise CliError(f"repo {repo!r} is not configured")


## Logs

COMPLETE_RE = re.compile(r"^Nightly used memory=.*timeout=.*$", re.MULTILINE)


def tail_log(client_config: ClientConfig, url: str) -> None:
    offset = 0
    recent = ""
    while True:
        req = urllib.request.Request(urllib.parse.urljoin(client_config.index_url, url))
        req.add_header("Range", f"bytes={offset}-")
        try:
            with client_config.open(req) as response:
                data = response.read()
                if response.status == 206:
                    chunk = data.decode("utf-8", errors="replace")
                    offset += len(data)
                elif len(data) <= offset:
                    chunk = ""
                else:
                    chunk = data[offset:].decode("utf-8", errors="replace")
                    offset = len(data)
        except urllib.error.HTTPError as exc:
            if exc.code == 416:
                chunk = ""
            else:
                raise
        if chunk:
            sys.stdout.write(chunk)
            sys.stdout.flush()
            recent = (recent + chunk)[-4096:]
            if COMPLETE_RE.search(recent):
                return
        time.sleep(1)


## Reports

REPORT_PATH_CUTOFF = datetime.date(2025, 12, 28)
PUBLISH_RE = re.compile(r"^Publishing report directory .* to .*/reports/([^/]+)/([^/\n]+)$", re.MULTILINE)


def find_report_url_in_log(repo: str, log_text: str) -> str | None:
    matched: str | None = None
    for match in PUBLISH_RE.finditer(log_text):
        if match.group(1) != repo:
            continue
        matched = REPORTS_PATH + repo + "/" + match.group(2)
    return matched


def fetch_published_report(
    client_config: ClientConfig,
    repo: str,
    name: str,
) -> str:
    log_text = client_config.fetch(log_url(client_config, name))
    report_url = find_report_url_in_log(repo, log_text)
    if report_url is None:
        raise CliError("No published report found in log.")
    return report_url


def fetch_manifest(client_config: ClientConfig, report_url: str) -> Manifest:
    return parse_manifest(client_config.fetch(report_url + "/nightly_info.json"))


def parse_manifest(text: str) -> Manifest:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CliError(f"invalid nightly_info.json: {exc}") from exc
    if not isinstance(payload, dict):
        raise CliError("nightly_info.json did not contain a JSON object")

    def optional_str(key: str) -> str | None:
        value = payload.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise CliError(f"nightly_info.json field {key} was not a string")
        return value

    def optional_time(key: str) -> str | None:
        value = optional_str(key)
        if value is None:
            return None
        try:
            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
        if parsed.utcoffset() == datetime.timedelta(0):
            return parsed.strftime("%Y-%m-%d %H:%M:%S UTC")
        return parsed.isoformat(sep=" ")

    def optional_duration() -> str | float | int | None:
        duration_human = payload.get("duration_human")
        if duration_human is not None:
            if not isinstance(duration_human, str):
                raise CliError("nightly_info.json field duration_human was not a string")
            return duration_human
        duration_seconds = payload.get("duration_seconds")
        if duration_seconds is None:
            return None
        if not isinstance(duration_seconds, int | float):
            raise CliError("nightly_info.json field duration_seconds was not a number")
        return duration_seconds

    files = payload.get("files")
    if not isinstance(files, list):
        raise CliError("nightly_info.json did not contain a files list")
    parsed_files: list[ManifestFile] = []
    for file_info in files:
        if not isinstance(file_info, dict):
            raise CliError("manifest file entry was not an object")
        path_value = file_info.get("path")
        gzip_value = file_info.get("gzip")
        if not isinstance(path_value, str):
            raise CliError("manifest file path was not a string")
        if not isinstance(gzip_value, bool):
            raise CliError("manifest gzip flag was not a boolean")
        path = Path(path_value)
        if path.is_absolute() or ".." in path.parts:
            raise CliError(f"unsafe manifest path {path_value!r}")
        if gzip_value and path_value.endswith(".gz"):
            parsed_files.append(ManifestFile(path_value, path_value.removesuffix(".gz")))
        elif gzip_value:
            parsed_files.append(ManifestFile(path_value + ".gz", path_value))
        else:
            parsed_files.append(ManifestFile(path_value, path_value))

    commit_short = optional_str("commit_short")
    image_url = optional_str("image_url")
    return Manifest(
        repo=optional_str("repo"),
        branch=optional_str("branch"),
        status=optional_str("status"),
        commit=commit_short if commit_short is not None else optional_str("commit"),
        started_at=optional_time("started_at"),
        finished_at=optional_time("finished_at"),
        duration=optional_duration(),
        report_url=optional_str("report_url"),
        log_url=optional_str("log_url"),
        image_url=image_url if image_url else None,
        files=parsed_files,
    )


CURL_PARALLEL_MAX = 32


def download_report_files(
    report_url: str,
    files: list[ManifestFile],
    output_dir: Path,
    client_config: ClientConfig,
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile("w", encoding="utf-8") as config_file:
        for file in files:
            remote_url = urllib.parse.urljoin(
                client_config.index_url,
                report_url + "/" + urllib.parse.quote(file.remote_path),
            )
            local_path = output_dir / file.remote_path
            print(f'url = "{remote_url}"', file=config_file)
            print(f'output = "{local_path}"', file=config_file)
        config_file.flush()
        subprocess.run(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--location",
                "--create-dirs",
                "--parallel",
                "--parallel-max",
                str(min(CURL_PARALLEL_MAX, max(1, len(files)))),
                "--user",
                f"{client_config.username}:{client_config.password}",
                "--config",
                config_file.name,
            ],
            check=True,
        )

    for file in files:
        if file.remote_path == file.local_path:
            continue
        remote_path = output_dir / file.remote_path
        local_path = output_dir / file.local_path
        with gzip.open(remote_path, "rb") as f_in, local_path.open("wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        remote_path.unlink()
    return len(files)


## Error formatting

def format_http_error(exc: urllib.error.HTTPError) -> str:
    try:
        payload = exc.read().decode("utf-8", errors="replace")
    except OSError:
        payload = ""
    finally:
        exc.close()
    text = " ".join(re.sub(r"<[^>]+>", " ", payload).split())
    if "Nightly sync already running" in text:
        return "Nightly sync already running"
    queued_match = re.search(r"Job nightly:[^ ]+ already queued", text)
    if queued_match is not None:
        return queued_match.group(0)
    if text:
        return f"HTTP {exc.code}: {text}"
    return f"HTTP {exc.code}: {exc.reason}"


def format_error(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return format_http_error(exc)
    if isinstance(exc, urllib.error.URLError):
        return f"failed to fetch: {exc}"
    if isinstance(exc, subprocess.CalledProcessError):
        command = exc.cmd[0] if isinstance(exc.cmd, list) and exc.cmd else "command"
        return f"{command} failed with exit status {exc.returncode}"
    return str(exc)

## Individual commands

def cmd_setup(url: str) -> int:
    username = input("Username: ").strip()
    if not username:
        raise CliError("username must not be empty")
    password = getpass.getpass("Password: ")
    if not password:
        raise CliError("password must not be empty")
    client_config = ClientConfig(url.strip(), username, password)
    index_state = parse_control_state(client_config.fetch_json(API_PATH))
    if not index_state.start_targets:
        raise CliError(f"could not find nightly controls at {client_config.index_url}")
    path = save_client_config(client_config)
    print(f"Saved CLI config to {path}")
    return 0


def wait_for_sync(client_config: ClientConfig, started: bool) -> None:
    timeout = SYNC_FINISH_TIMEOUT if started else SYNC_START_TIMEOUT
    deadline = time.monotonic() + timeout
    while True:
        index_state = parse_control_state(client_config.fetch_json(API_PATH))
        if index_state.sync_disabled:
            if not started:
                started = True
                deadline = time.monotonic() + SYNC_FINISH_TIMEOUT
        elif started:
            return
        elif time.monotonic() >= deadline:
            raise CliError("Nightly sync did not start")
        if time.monotonic() >= deadline:
            raise CliError("Nightly sync did not finish before timeout")
        time.sleep(SYNC_POLL_INTERVAL)


def cmd_sync(client_config: ClientConfig, wait: bool = False) -> int:
    try:
        client_config.post(SYNC_PATH, {})
    except urllib.error.HTTPError as exc:
        if exc.code != 409:
            raise
        if not wait:
            raise CliError("Nightly sync already running") from exc
        wait_for_sync(client_config, started=True)
    else:
        if wait:
            wait_for_sync(client_config, started=False)
    return 0


def cmd_start(client_config: ClientConfig, repo: str, branch: str) -> int:
    index_state = parse_control_state(client_config.fetch_json(API_PATH))
    target = resolve_start_target(index_state, repo, branch)
    if index_state.sync_disabled:
        raise CliError("Nightly sync already running")
    if target.disabled:
        raise CliError(f"Branch {target.branch} on {target.repo} already queued")
    client_config.post(START_PATH, {"repo": target.repo, "branch": target.branch})
    return 0


def cmd_download(client_config: ClientConfig, repo: str, selector: RunSelector) -> int:
    run_log = next(matching_run_logs(iter_entries(client_config, repo, selector), repo, selector), None)
    if run_log is None:
        raise CliError("No matching log found.")

    report_url = fetch_published_report(client_config, repo, run_log.name)
    manifest = fetch_manifest(client_config, report_url)
    output_dir = Path(urllib.parse.urlsplit(report_url).path.rstrip("/")).name
    file_count = download_report_files(report_url, manifest.files, Path(output_dir), client_config)
    print(f"Downloaded {file_count} files to {output_dir}/")
    return 0


def cmd_list(
    client_config: ClientConfig,
    repo: str,
    selector: RunSelector,
) -> int:
    entries = list(itertools.islice(
        matching_run_logs(iter_entries(client_config, repo, selector), repo, selector),
        LOG_PAGE_SIZE,
    ))
    entries.reverse()
    if not entries:
        raise CliError(f"No runs found for repo {repo}.")
    for run in entries:
        print(f"{run.date:10} {run.time:8} {run.branch}")
    return 0


def cmd_log(client_config: ClientConfig, repo: str, selector: RunSelector, follow: bool) -> int:
    run_log = next(matching_run_logs(iter_entries(client_config, repo, selector), repo, selector), None)
    if run_log is None:
        raise CliError("No matching log found.")
    if follow:
        tail_log(client_config, log_url(client_config, run_log.name))
    else:
        sys.stdout.write(client_config.fetch(log_url(client_config, run_log.name)))
    return 0


def cmd_status(client_config: ClientConfig, repo: str, selector: RunSelector) -> int:
    run_log = next(matching_run_logs(iter_entries(client_config, repo, selector), repo, selector), None)
    if run_log is None:
        raise CliError("No matching log found.")
    log_text = client_config.fetch(log_url(client_config, run_log.name))
    report_url = find_report_url_in_log(repo, log_text)
    if report_url is None:
        if datetime.date.fromisoformat(run_log.date) < REPORT_PATH_CUTOFF:
            print("No report (run is too old). Run `uvx nightlies log` to view log details")
        else:
            print("No report. Run `uvx nightlies log` to view log details")
        return 0
    manifest = fetch_manifest(client_config, report_url)
    print(manifest.text())
    return 0


def cmd_open(client_config: ClientConfig, repo: str, selector: RunSelector) -> int:
    run_log = next(matching_run_logs(iter_entries(client_config, repo, selector), repo, selector), None)
    if run_log is None:
        raise CliError("No matching log found.")
    url = urllib.parse.urljoin(client_config.index_url, fetch_published_report(client_config, repo, run_log.name))
    if not webbrowser.open_new(url):
        raise CliError(f"could not open browser for {url}")
    return 0


## Main method and flag handling

def normalize_time(value: str | None) -> str | None:
    if value is None:
        return None
    digits = value.replace(":", "")
    if len(digits) == 6 and digits.isdigit():
        return ":".join([digits[:2], digits[2:4], digits[4:6]])
    return value


def add_run_selector_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("branch", nargs="?", default=None, help="Branch name.")
    parser.add_argument(
        "date",
        nargs="?",
        default=None,
        help="Run date as YYYY, YYYY-MM, or YYYY-MM-DD.",
    )
    parser.add_argument("time", nargs="?", default=None, type=normalize_time, help="Run time as HH:MM:SS or HHMMSS.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Query nightly.cs.washington.edu logs and reports.")
    parser.add_argument("-C", dest="cwd", default=".", help="Change to this directory first.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup_parser = subparsers.add_parser("setup", help="Save the nightly URL and credentials.")
    setup_parser.add_argument("url", help="Nightly base URL, such as https://nightly.cs.washington.edu/.")

    sync_parser = subparsers.add_parser("sync", help="Start a sync-with-GitHub dry run from the web UI.")
    sync_parser.add_argument("--wait", action="store_true", help="Wait until the sync finishes.")

    list_parser = subparsers.add_parser("list", help="List runs for a repo.")
    add_run_selector_args(list_parser)

    start_parser = subparsers.add_parser("start", help="Start a single repo branch run from the web UI.")
    start_parser.add_argument("branch", nargs="?", default=None, help="Branch name.")

    log_parser = subparsers.add_parser("log", help="Print a log for a repo branch.")
    log_parser.add_argument("-f", action="store_true", dest="follow", help="Follow the log until it completes.")
    add_run_selector_args(log_parser)

    status_parser = subparsers.add_parser("status", help="Show published report status for a repo branch run.")
    add_run_selector_args(status_parser)

    download_parser = subparsers.add_parser("download", help="Download a published report for a repo branch run.")
    add_run_selector_args(download_parser)

    open_parser = subparsers.add_parser("open", help="Open a published report for a repo branch run in a browser.")
    add_run_selector_args(open_parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        os.chdir(args.cwd)
        if args.command == "setup":
            return cmd_setup(args.url)
        client_config = load_client_config()
        if args.command == "sync":
            return cmd_sync(client_config, args.wait)
        repo = infer_repo(".")
        if args.command in {"log", "start", "status", "open"} and args.branch is None:
            args.branch = current_branch(".")
        if args.command == "start":
            return cmd_start(client_config, repo, args.branch)
        selector = RunSelector(args.branch, args.date, args.time)
        if args.command == "list":
            return cmd_list(client_config, repo, selector)
        if args.command == "log":
            return cmd_log(client_config, repo, selector, args.follow)
        if args.command == "status":
            return cmd_status(client_config, repo, selector)
        if args.command == "download":
            return cmd_download(client_config, repo, selector)
        if args.command == "open":
            return cmd_open(client_config, repo, selector)
        raise CliError(f"unknown command {args.command}")
    except (MissingClientConfig, InvalidClientConfig) as exc:
        print(f"error: {exc}. Run `cli setup <url>` to fix.", file=sys.stderr)
        return 1
    except (CliError, EOFError, OSError, gzip.BadGzipFile, subprocess.CalledProcessError, urllib.error.URLError) as exc:
        print(f"error: {format_error(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
