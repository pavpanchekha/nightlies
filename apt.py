from dataclasses import dataclass
from pathlib import Path
from typing import List, Protocol, Sequence
import subprocess

import re


class AptRunner(Protocol):
    dryrun: bool

    def log(self, level: int, message: str) -> None: ...

    def exec(
        self,
        level: int,
        cmd: Sequence[str | Path],
    ) -> subprocess.CompletedProcess[bytes]: ...

APT_LINE_RE = re.compile(r"^(\d+) upgraded, (\d+) newly installed, (\d+) to remove and (\d+) not upgraded\.$", re.MULTILINE)
APT_INST_RE = re.compile(r"^Inst (\S+)(?: \[([^\]]+)\])? \(([^)]+)\)")
APT_REMV_RE = re.compile(r"^Remv (\S+) \[([^\]]+)\]")

NEW_PACKAGE_VERSION = "<none>"
REMOVED_PACKAGE_VERSION = "<removed>"


@dataclass(frozen=True)
class AptPackageUpdate:
    package: str
    before: str
    after: str

def _format_cmd(cmd: Sequence[object]) -> str:
    return " ".join(str(part) for part in cmd)


def _apt_source_files(
    source_list: Path = Path("/etc/apt/sources.list"),
    sources_dir: Path = Path("/etc/apt/sources.list.d"),
) -> List[Path]:
    files: List[Path] = []
    if source_list.is_file():
        files.append(source_list)
    if sources_dir.is_dir():
        for path in sorted(sources_dir.iterdir()):
            if path.is_file():
                files.append(path)
    return files

def _has_repository(
    repo: str,
    source_list: Path = Path("/etc/apt/sources.list"),
    sources_dir: Path = Path("/etc/apt/sources.list.d"),
) -> bool:
    if not repo.startswith("ppa:") or repo.count("/") != 1:
        return False

    ppa_path = f"/{repo[4:]}/ubuntu"

    for path in _apt_source_files(source_list, sources_dir):
        try:
            contents = path.read_text(errors="ignore")
        except OSError:
            continue
        for line in contents.splitlines():
            if not line.startswith("URIs:"):
                continue
            uri = line.removeprefix("URIs:").strip()
            if ppa_path in uri:
                return True
    return False


def add_repositories(runner: AptRunner, repos: Sequence[str]) -> List[str]:
    failed: List[str] = []
    for repo in repos:
        if _has_repository(repo):
            runner.log(1, f"Apt repository {repo} already present; skipping")
            continue
        runner.log(1, f"Adding apt repository {repo}")
        if runner.dryrun:
            continue
        try:
            runner.exec(2, ["sudo", "add-apt-repository", "--yes", repo])
        except subprocess.CalledProcessError as e:
            failed.append(repo)
            runner.log(
                1,
                f"Failed to add apt repository {repo}: process {_format_cmd(e.cmd)} returned error code {e.returncode}",
            )
        except OSError as e:
            failed.append(repo)
            runner.log(1, f"Failed to add apt repository {repo}: {e}")
    return failed


def _parse_updates(stdout: str) -> List[AptPackageUpdate]:
    updates: List[AptPackageUpdate] = []
    for line in stdout.splitlines():
        inst = APT_INST_RE.match(line)
        if inst:
            package, before, after_info = inst.group(1, 2, 3)
            after = after_info.split(" ", 1)[0]
            updates.append(AptPackageUpdate(package, before or NEW_PACKAGE_VERSION, after))
            continue

        remv = APT_REMV_RE.match(line)
        if remv:
            package, before = remv.group(1, 2)
            updates.append(AptPackageUpdate(package, before, REMOVED_PACKAGE_VERSION))

    return updates


def check_updates(runner: AptRunner, pkgs: List[str]) -> List[AptPackageUpdate]:
    runner.log(1, f"Checking for updates to apt packages {' '.join(pkgs)}")
    stdout = runner.exec(2, ["sudo", "apt", "install", "--dry-run"] + pkgs).stdout.decode("latin1")

    # Parse the `apt` output, ugh
    match = APT_LINE_RE.search(stdout)
    if not match:
        raise IOError("apt: Could not find package line in `apt` results")

    num_u, num_i, num_r, _ = match.group(1, 2, 3, 4)
    if not (int(num_u) or int(num_i) or int(num_r)):
        return []

    updates = _parse_updates(stdout)
    if not updates:
        raise IOError("apt: Could not parse package updates from `apt` results")
    return updates

def install(runner: AptRunner, pkgs: List[str]) -> None:
    runner.log(1, f"Installing apt packages {' '.join(pkgs)}")
    runner.exec(2, ["sudo", "apt", "install", "--yes"] + pkgs)
