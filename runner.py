#!/usr/bin/env python3

from typing import Any, Dict, List, Optional, Sequence
from datetime import datetime, timezone
from pathlib import Path
import argparse, gzip, json, shlex, shutil, subprocess, sys, tempfile, time
import signal
import os
import config, slack

def log(msg: str) -> None:
    print(msg, flush=True)

def format_time(ts: float) -> str:
    t = float(ts)
    if t < 120:
        return f"{t:.1f}s"
    elif t < 120*60:
        return f"{t/60:.1f}m"
    else:
        return f"{t/60/60:.1f}h"

def format_cmd(s: Sequence[str | Path]) -> str:
    return shlex.join([str(part) for part in s])

def run(cmd: Sequence[str | Path], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
    log(f"Executing {format_cmd(cmd)}")
    return subprocess.run(cmd, **kwargs)

def parse_time(to: str | None) -> float | None:
    if to is None:
        return to
    units = {"hr": 3600, "h": 3600, "min": 60, "m": 60, "sec": 1, "s": 1}
    for unit, multiplier in units.items():
        if to.endswith(unit):
            return float(to[:-len(unit)]) * multiplier
    return float(to)

def format_size(size: int) -> str:
    units = ["KB", "MB", "GB", "TB", "PB"]
    s = float(size) / 1024
    for unit in units:
        if s < 1024:
            break
        s /= 1024
    return f"{s:.2f}{unit}"

def tree_size(root: Path) -> tuple[int, Path | None, int]:
    total = 0
    biggest = None
    biggest_size = 0
    for dirpath, _, files in root.walk():
        for name in files:
            path = dirpath / name
            size = path.lstat().st_size
            total += size
            if size > biggest_size:
                biggest_size = size
                biggest = path
    return total, biggest and biggest.relative_to(root), biggest_size

def copything(src: Path, dst: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)

def gzip_matching_files(directory: Path, globs: List[str]) -> set[Path]:
    compressed: set[Path] = set()
    for path in directory.rglob("*"):
        if path.is_file() and any(path.match(g) for g in globs):
            gz_path = path.with_suffix(path.suffix + ".gz")
            compressed.add(gz_path.relative_to(directory))
            with path.open("rb") as f_in, gzip.open(gz_path, "wb", compresslevel=9) as f_out:
                shutil.copyfileobj(f_in, f_out)
            path.unlink()
    return compressed

def report_files(directory: Path, compressed: set[Path]) -> List[Dict[str, Any]]:
    files: List[Dict[str, Any]] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        report_path = path.relative_to(directory)
        files.append({
            "path": str(report_path),
            "gzip": report_path in compressed,
        })
    return files

def read_metadata(metadata_file: Path) -> Dict[str, Any]:
    if metadata_file.exists():
        with metadata_file.open() as f:
            try:
                data: Dict[str, Any] = json.load(f)
                return data
            except json.JSONDecodeError:
                pass
    return {}

def save_metadata(metadata_file: Path, data: Dict[str, Any]) -> None:
    with metadata_file.open("w") as f:
        json.dump(data, f)

def format_time_iso_utc(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def write_nightly_info(
    branch_config: config.BranchConfig,
    *,
    commit: str,
    status: str,
    started_at: datetime,
    finished_at: datetime,
    log: str,
    log_url: str,
    report_url: str,
    image_url: str | None,
    compressed: set[Path],
) -> None:
    duration_seconds = (finished_at - started_at).total_seconds()
    assert branch_config.report_dir is not None
    with (branch_config.report_dir / "nightly_info.json").open("w") as f:
        json.dump({
            "repo": branch_config.repo_name,
            "branch": branch_config.branch_name,
            "branch_filename": branch_config.branch_filename,
            "commit": commit,
            "commit_short": commit[:8],
            "status": status,
            "started_at": format_time_iso_utc(started_at),
            "finished_at": format_time_iso_utc(finished_at),
            "duration_seconds": duration_seconds,
            "duration_human": format_time(duration_seconds),
            "log": log,
            "log_url": log_url,
            "report_url": report_url,
            "image_url": image_url,
            "files": report_files(branch_config.report_dir, compressed),
        }, f)

def run_branch(bc: config.BranchConfig, log_name: str | None) -> int:
    log(f"Running branch {bc.branch_name} on repo {bc.repo_name}")
    info: Dict[str, str] = {}
    slack_output = slack.make_output(bc.secrets, bc.slack_spec, bc.repo_name)
    start: Optional[datetime] = None
    out: Optional[str] = None

    if bc.base_url:
        assert log_name is not None
        import urllib.parse
        info["logurl"] = bc.base_url + "logs/" + urllib.parse.quote(log_name)

    def handle_sigterm(signum, frame):
        log(f"Received signal {signum}")
        info["result"] = "*killed*"
        if start is not None:
            info["time"] = format_time((datetime.now(timezone.utc) - start).total_seconds())
        log("Posting killed result to slack")
        if slack_output:
            try:
                slack_output.post(bc.branch_name, info)
            except slack.SlackError as e:
                log(f"Slack error: {e}")
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, handle_sigterm)

    try:
        run(["git", "-C", bc.branch_dir, "reset", "--hard", bc.revision], check=True)
        submodule_cmd: List[str | Path] = [
            "git", "-C", bc.branch_dir, "submodule", "update", "--init", "--recursive", "--force",
        ]
        if bc.shallow:
            submodule_cmd.append("--depth=1")
        run(submodule_cmd, check=True)

        out = run(
            ["git", "-C", bc.branch_dir, "rev-parse", bc.revision],
            capture_output=True, check=True
        ).stdout.decode("ascii").strip()

        start = datetime.now(timezone.utc)
        to = parse_time(bc.timeout)
        cmd = ["make", "-C", str(bc.branch_dir), "nightly"]

        if bc.report_dir:
            if bc.report_dir.exists():
                shutil.rmtree(bc.report_dir, ignore_errors=True)
            bc.report_dir.mkdir(parents=True, exist_ok=True)

        result = run(cmd, timeout=to)
        if result.returncode:
            raise subprocess.CalledProcessError(result.returncode, cmd)

    except subprocess.TimeoutExpired as e:
        log(f"Run on branch {bc.branch_name} timed out after {format_time(e.timeout)}")
        status = "timeout"
    except subprocess.CalledProcessError:
        status = "failure"
    else:
        log(f"Successfully ran on branch {bc.branch_name}")
        status = "success"

    if start is not None:
        assert out is not None
        metadata = read_metadata(bc.metadata_file)
        metadata["commit"] = out
        metadata["time"] = time.time()
        save_metadata(bc.metadata_file, metadata)

        if bc.report_dir and bc.report_dir.exists():
            try:
                compressed: set[Path] = set()
                if bc.gzip:
                    log(f"GZipping all {bc.gzip} files")
                    compressed = gzip_matching_files(bc.report_dir, shlex.split(bc.gzip))

                total, biggest, _ = tree_size(bc.report_dir)
                if bc.warn_report and total > bc.warn_report:
                    msg = (
                        f"Report size {format_size(total)} exceeds limit {format_size(bc.warn_report)}"
                        f"; largest file `{biggest}`"
                    )
                    log(f"Report `{bc.branch_name}` is {format_size(total)}; largest file `{biggest}`")
                    if slack_output:
                        slack_output.warn("report-size", msg)

                if "url" not in info and bc.base_url:
                    assert log_name is not None
                    name = f"{int(time.time())}:{bc.branch_filename}:{out[:8]}"
                    dest_dir = bc.reports_dir / bc.repo_name / name
                    report_url = bc.base_url + "reports/" + bc.repo_name + "/" + name

                    if bc.report_dir.exists():
                        image_url = None
                        if bc.image_file and bc.image_file.exists():
                            path = bc.image_file.relative_to(bc.report_dir)
                            image_url = report_url + "/" + str(path)
                        if status == "success":
                            write_nightly_info(
                                bc,
                                commit=out,
                                status=status,
                                started_at=start,
                                finished_at=datetime.now(timezone.utc),
                                log=log_name,
                                log_url=bc.base_url + "logs/" + urllib.parse.quote(log_name),
                                report_url=report_url,
                                image_url=image_url,
                                compressed=compressed,
                            )
                        log(f"Publishing report directory {bc.report_dir} to {dest_dir}")
                        copything(bc.report_dir, dest_dir)
                        info["url"] = report_url
                        if bc.image_file and bc.image_file.exists():
                            log(f"Linking image file {bc.image_file}")
                            assert image_url is not None
                            info["img"] = image_url
                        shutil.rmtree(bc.report_dir, ignore_errors=True)
                    else:
                        log(f"Report directory {bc.report_dir} does not exist")
            except OSError as e:
                msg = f"Error saving report: {e}"
                log(f"Error saving report for `{bc.branch_name}`: {e}")
                if slack_output:
                    slack_output.warn("broken-report", msg)

    total, biggest, _ = tree_size(bc.branch_dir)
    if bc.warn_branch and total > bc.warn_branch:
        msg = (
            f"Branch size {format_size(total)} exceeds limit {format_size(bc.warn_branch)}"
            f"; largest file `{biggest}`"
        )
        log(msg)
        if slack_output:
            slack_output.warn("branch-size", msg)

    if log_name is not None:
        log_file = bc.logs_dir / log_name
        size = log_file.stat().st_size if log_file.exists() else 0
        if bc.warn_log and size > bc.warn_log:
            msg = (
                f"Log size {format_size(size)} exceeds limit {format_size(bc.warn_log)}"
            )
            log(msg)
            if slack_output:
                slack_output.warn("log-size", msg)

    info["result"] = f"*{status}*" if status != "success" else "success"
    if start is not None:
        info["time"] = format_time((datetime.now(timezone.utc) - start).total_seconds())

    if slack_output:
        log("Posting results of run to slack!")
        try:
            slack_output.post(bc.branch_name, info)
        except slack.SlackError as e:
            log(f"Slack error: {e}")

    job_id = os.environ.get("SLURM_JOB_ID")
    if job_id is None:
        log("SLURM_JOB_ID not set; skipping memory usage collection")
    else:
        output = run(
            ["sstat", "--noheader", "-j", f"{job_id}.batch", "--format=MaxRSS"],
            capture_output=True, check=True,
        ).stdout.decode("ascii", errors="replace").strip()
        assert output, f"sstat returned empty line: {output!r}"
        assert "\n" not in output, f"sstat returned multiple lines: {output!r}"
        max_rss = config.parse_size(output)
        assert max_rss is not None, f"sstat returned unknown MaxRSS: {output!r}"
        log(f"Nightly used memory={format_size(max_rss).lower()}, timeout={info['time']}")

    return 1 if status != "success" else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a branch nightly on SLURM or Northflank")
    parser.add_argument("--mode", choices=["slurm", "northflank"], default="slurm")
    parser.add_argument("config_file", nargs="?")
    parser.add_argument("repo", nargs="?")
    parser.add_argument("branch", nargs="?")
    parser.add_argument("log_name", nargs="?")
    args = parser.parse_args()
    runner_args = (args.config_file, args.repo, args.branch, args.log_name)

    if args.mode == "slurm":
        if any(arg is None for arg in runner_args):
            parser.error("slurm mode requires: <config_file> <repo> <branch> <log_name>")
        config_file, repo, branch, log_name = runner_args
        assert config_file is not None and repo is not None and branch is not None and log_name is not None
        cfg = config.Config(config_file)
        bc = config.BranchConfig(cfg, repo, branch)
        return run_branch(bc, log_name)

    if any(arg is not None for arg in runner_args):
        parser.error("northflank mode does not take positional arguments")
    repo = os.environ.get("NIGHTLIES_REPO")
    commit = os.environ.get("NIGHTLIES_COMMIT")
    if not repo or not commit:
        parser.error("northflank mode requires NIGHTLIES_REPO and NIGHTLIES_COMMIT")

    with tempfile.TemporaryDirectory(prefix="nightlies-") as directory:
        bc = config.BranchConfig.northflank(repo, commit, Path(directory))
        bc.repo_dir.mkdir(parents=True)
        run(["git", "init", bc.branch_dir], check=True)
        run([
            "git", "-C", bc.branch_dir, "remote", "add", "origin",
            config.repo_to_url(repo, protocol="https"),
        ], check=True)
        run([
            "git", "-C", bc.branch_dir, "fetch", "--depth=1", "--filter=blob:none", "origin", commit,
        ], check=True)
        return run_branch(bc, None)


if __name__ == "__main__":
    sys.exit(main())
