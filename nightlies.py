#!/usr/bin/env python3

from typing import Any, List, Dict, Union, Optional, cast, Sequence
import os, sys, subprocess, time, gzip
from datetime import datetime
from pathlib import Path
import configparser
import json
import shlex, shutil
import slack, apt
from config import parse_cores, parse_size, format_size_slurm, repo_to_url, short_repo_name
import urllib.request, urllib.error

def format_cmd(s : Sequence[Union[str, Path]]) -> str:
    if hasattr(shlex, "join"):
        return shlex.join([str(part) for part in s])
    else: # Compatibility with old Python 3
        return " ".join([
            str(part) if " " not in str(part) else '"{}"'.format(part)
            for part in s
        ])

SBATCH_BASE = [
    "sbatch",
    "--export=ALL,TERM=dumb",
    "--parsable",
]

REPO_BADGES = [
    "baseline", # Run this branch if you run anything else
    "always", # Run this branch every day
    "never", # Never run this branch
]

class NightlyRunner:
    def __init__(self, config_file : str) -> None:
        self.config_file = Path(config_file)
        self.self_dir = Path(__file__).resolve().parent
        self.data : Any = None
        self.log_path: Optional[Path] = None
        self.start: Optional[datetime] = None

    def update_system_repo(self, dir : str, repo : str, branch : str) -> None:
        if not Path(dir).is_dir():
            self.log(1, f"Downloading system {repo} repository {dir}")
            self.exec(2, ["git", "-C", dir, "clone", "--recursive",
                          "--branch", branch, "--", repo_to_url(repo), dir])
            self.restart()

        self.log(1, f"Updating system {repo} repository in {dir}")
        try:
            conf_commit = self.exec(2, ["git", "-C", dir, "rev-parse", "HEAD"]).stdout
            self.log(2, f"Commit {conf_commit.decode().strip()}")
            self.exec(2, ["git", "-C", dir, "fetch", "origin", "--prune"])
            self.exec(2, ["git", "-C", dir, "reset", "--hard", "origin/" + branch])
            conf_commit2 = self.exec(2, ["git", "-C", dir, "rev-parse", "HEAD"]).stdout
            self.log(2, f"Commit {conf_commit2.decode().strip()}")
        except subprocess.CalledProcessError as e:
            self.log(0, f"Process {format_cmd(e.cmd)} returned error code {e.returncode}")
            sys.exit(1)
        if conf_commit == conf_commit2:
            self.log(1, f"System {dir} repository up to date")
        else:
            self.log(1, f"System {dir} repository updated; will need to restart")
            self.restart()

    def load(self) -> None:
        assert self.config_file.is_file(), f"Configuration file {self.config_file} is not a file"
        self.config = configparser.ConfigParser()
        self.config.read(str(self.config_file))
        self.repos : List[Repository] = []

        defaults = self.config.defaults()
        self.base_url = defaults.get("baseurl")
        if self.base_url:
            self.base_url = self.base_url.rstrip("/") + "/"
        self.dir = Path(defaults.get("repos", ".")).resolve()
        self.log_dir = Path(defaults.get("logs", "logs")).resolve()
        self.dryrun = "dryrun" in defaults
        self.pid_file = Path(defaults.get("pid", "running.pid")).resolve()
        self.config_file = Path(defaults.get("conffile", str(self.config_file))).resolve()
        self.report_dir = Path(defaults.get("reports", "reports")).resolve()

        self.secrets = configparser.ConfigParser()
        if defaults.get("secrets"):
            for file in Path(defaults["secrets"]).iterdir():
                if not file.name.endswith(".conf"): continue
                with file.open() as f:
                    self.secrets.read_file(f, source=f.name)

        for name in self.config.sections():
            self.repos.append(Repository(self, name, self.config[name]))

    def update(self) -> None:
        if not self.config.getboolean("DEFAULT", "update", fallback=False): return

        runner_repo = self.config.defaults().get("self", "pavpanchekha/nightly-runner")
        runner_branch = self.config.defaults().get("selfbranch", "main")
        self.update_system_repo(".", runner_repo, runner_branch)

        conf_dir = os.path.dirname(self.config_file)
        conf_repo = self.config.defaults()["conf"]
        conf_branch = self.config.defaults().get("confbranch", "main")
        self.update_system_repo(conf_dir, conf_repo, conf_branch)

        sec_repo = self.config.defaults()["secrets"]
        sec_branch = self.config.defaults().get("secretsbranch", "main")
        self.update_system_repo("secrets", sec_repo, sec_branch)

    def restart(self) -> None:
        self.log(0, "Restarting nightly run due to updated system repositories")
        os.execv(sys.executable, ["python3"] + sys.argv)

    def exec(self, level : int, cmd : Sequence[Union[str, Path]]) -> subprocess.CompletedProcess[bytes]:
        self.log(level, f"Executing {format_cmd(cmd)}")
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True)

    def log(self, level : int, s : str) -> None:
        if self.log_path is None or self.start is None:
            print(f"{datetime.now().isoformat()}\t{'    ' * level}{s}", file=sys.stderr)
            return
        with self.log_path.open("at") as f:
            f.write("{}\t{}{}\n".format(datetime.now() - self.start, "    " * level, s))

    def save(self) -> None:
        with self.pid_file.open("w") as f:
            json.dump(self.data, f)

    def load_pid(self) -> None:
        try:
            with self.pid_file.open("r") as f:
                self.data = json.load(f)
            self.log_path = Path(cast(str, self.data["log"]))
            self.start = datetime.fromisoformat(cast(str, self.data["start"]))
        except OSError:
            return
        except json.decoder.JSONDecodeError:
            self.data = {"dead": True}

    def try_lock(self) -> bool:
        try:
            self.pid_file.touch(exist_ok=False)
            return True
        except FileExistsError:
            return False

    def lock(self) -> bool:
        if self.try_lock():
            return True
        else:
            try:
                with self.pid_file.open("r") as f:
                    current_process = json.load(f)
                    if current_process.get("pid") == os.getpid():
                        self.log(1, "Reusing existing lockfile after restart")
                        return True
                    self.log(0, f"Nightly already running on pid {current_process['pid']}")
            except (OSError, json.decoder.JSONDecodeError):
                self.log(0, f"Nightly already running")

            if self.config.getboolean("DEFAULT", "wait", fallback=True):
                while True:
                    if self.try_lock():
                        return True
                    else:
                        self.log(1, f"Sleeping for 15 minutes...")
                        time.sleep(15 * 60)
            else:
                return False


    def run(self) -> None:
        self.start = datetime.now()
        name = f"{self.start:%Y-%m-%d}-{self.start:%H%M%S-%f}.log"
        self.log_path = self.log_dir / name
        self.log(0, f"Nightly script starting up at {self.start:%H:%M}")
        self.log(0, f"Loaded configuration file {self.config_file}")
        if not self.lock():
            return

        self.data = {
            "pid": os.getpid(),
            "start": self.start.isoformat(),
            "config": str(Path(self.config_file).resolve()),
            "log": str(self.log_path),
        }
        self.save()

        self.update()

        if self.dryrun:
            self.log(0, "Running in dry-run mode. No nightlies will be executed.")

        if Path(".git/info").exists():
            with open(".git/info/exclude", "wt") as f:
                for repo in self.repos:
                    f.write(repo.dir.name + "\n")

        plan : List[Branch] = []
        for repo in self.repos:
            try:
                self.data["repo"] = repo.name
                self.save()
                repo.load()
                repo.plan()
                plan.extend(repo.runnable)
            except subprocess.CalledProcessError as e:
                msg = f"Process {format_cmd(e.cmd)} returned error code {e.returncode}"
                self.log(1, msg)
                if repo.slack:
                    try:
                        repo.slack.fatal(msg)
                    except slack.SlackError as e:
                        self.log(2, f"Slack error: {e}")
            except OSError as e:
                msg = f"Fatal error: {str(e)}"
                self.log(1, msg)
                if repo.slack:
                    try:
                        repo.slack.fatal(msg)
                    except slack.SlackError as e:
                        self.log(2, f"Slack error: {e}")
            finally:
                del self.data["repo"]

        for branch in plan:
            try:
                date = datetime.now()
                job_name = f"nightly:{branch.repo.name}:{branch.filename}"
                log_name = f"{date:%Y-%m-%d}-{date:%H%M%S-%f}-{branch.repo.name}-{branch.filename}.log"

                self.data["repo"] = branch.repo.name
                self.save()

                if self.dryrun:
                    self.log(0, f"Dry-run: skipping branch {branch.name} on repo {branch.repo.name}")
                    continue

                if "queued" in branch.badges:
                    self.log(1, f"Job {job_name} already queued; skipping")
                    continue

                repo_full_name = branch.repo.gh_name or branch.repo.name
                log_path = self.log_dir / log_name
                wrap_cmd = shlex.join([
                    sys.executable, "runner.py",
                    str(self.config_file), repo_full_name, branch.name, log_name
                ])

                resource_args: List[str] = []
                if branch.repo.cores is None:
                    resource_args.append("--exclusive")
                else:
                    resource_args.append(f"--cpus-per-task={branch.repo.cores}")
                if branch.repo.memory is not None:
                    resource_args.append(f"--mem={format_size_slurm(branch.repo.memory)}")

                cmd = SBATCH_BASE + resource_args + [
                    f"--job-name={job_name}",
                    f"--comment={log_name}",
                    f"--output={log_path}",
                    f"--error={log_path}",
                    f"--wrap={wrap_cmd}",
                ]
                result = self.exec(1, cmd)
                self.log(2, f"Submitted job {result.stdout.decode().strip()}")
            except subprocess.CalledProcessError as e:
                error_output = e.stdout.decode().strip() if e.stdout else ""
                msg = f"Failed to queue branch {branch.name}: {error_output or f'exit code {e.returncode}'}"
                self.log(1, msg)
                if branch.slack:
                    try:
                        branch.slack.fatal(msg)
                    except slack.SlackError as e:
                        self.log(2, f"Slack error: {e}")
            finally:
                del self.data["repo"]

                # del self.data["runs_done"]
                # del self.data["runs_total"]
                self.save()

        self.pid_file.unlink()
        self.log(0, "Finished submitting jobs")

class Repository:
    def __init__(self, runner : NightlyRunner, name : str, configuration : configparser.SectionProxy):
        self.runner = runner
        self.config = configuration
        self.runnable : List[Branch] = []

        self.slack = slack.make_output(self.runner.secrets, configuration.get("slack"), name)

        if self.config.get("url"): # Reserved for local testing
            self.url = self.config["url"]
            self.gh_name : Optional[str] = None
        else:
            self.url = repo_to_url(name)
            self.gh_name = name

        self.name = short_repo_name(name)
        self.dir = runner.dir / self.name
        self.checkout = self.dir / ".checkout"
        self.status = self.dir / ".status"
        self.ignored_files = {
            self.dir / path
            for path in shlex.split(self.config.get("ignore", ""))
        } | set([self.checkout, self.status])
        self.report_dir_name = configuration.get("report")
        self.image_file_name = configuration.get("image")
        self.cores = parse_cores(configuration.get("cores"))
        self.memory = parse_size(configuration.get("memory"))
        
        self.branches : Dict[str, Branch] = {}

    def list_branches(self) -> List[str]:
        git_branch = self.runner.exec(2, ["git", "-C", self.checkout, "branch", "-r"])
        return [
            branch.split("/", 1)[-1] for branch
            in git_branch.stdout.decode("utf8").strip().split("\n")
            if "->" not in branch
        ]

    def list_pr_branches(self) -> Dict[str, int]:
        if not self.gh_name: return {}
        pulls_url = f"https://api.github.com/repos/{self.gh_name}/pulls"
        try:
            with urllib.request.urlopen(pulls_url) as data:
                pr_data = json.load(data)
        except urllib.error.HTTPError:
            return {}
        out : Dict[str, int] = {}
        for pr in pr_data:
            if pr["head"]["repo"]["full_name"] == self.gh_name:
                out[pr["head"]["ref"]] = pr["number"]
        return out

    def get_pr_link(self, pr : int) -> str:
        if not self.gh_name: raise ValueError("Not a Github repository")
        return f"https://github.com/{self.gh_name}/pull/{pr}"

    def load(self) -> None:
        self.runner.log(0, "Beginning nightly run for " + self.name)
        self.dir.mkdir(parents=True, exist_ok=True)

        ppas = self.config.get("ppa", "").split()
        if ppas:
            failed_ppas = apt.add_repositories(self.runner, ppas)
            if failed_ppas:
                joined = ", ".join(failed_ppas)
                msg = (f"Failed to add apt repository {joined}"
                       if len(failed_ppas) == 1
                       else f"Failed to add apt repositories {joined}")
                self.runner.log(1, msg)
                if self.slack:
                    self.slack.warn("ppa", msg)

        pkgs = self.config.get("apt", "").split()
        updates = apt.check_updates(self.runner, pkgs) if pkgs and not self.runner.dryrun else []
        if updates:
            apt.install(self.runner, pkgs)
            detail = ", ".join(f"{u.package} ({u.before} -> {u.after})" for u in updates)
            msg = f"Updated apt packages: {detail}"
            self.runner.log(1, msg)
            if self.slack:
                self.slack.warn("apt", msg)

        if not self.checkout.is_dir():
            self.runner.log(1, "Checking out base repository for " + self.name)
            self.runner.exec(2, ["git", "clone", "--recursive", self.url, self.checkout])
            self.runner.exec(2, ["git", "-C", self.checkout, "checkout", "--detach"])

        self.runner.log(1, "Updating branches for " + self.name)
        self.runner.exec(2, ["git", "-C", self.checkout, "fetch", "origin", "--prune", "--no-tags", "--recurse-submodules=on-demand"])

        if "branches" in self.config:
            all_branches = self.config["branches"].split()
        else:
            all_branches = self.list_branches()
        self.branches = { branch: Branch(self, branch) for branch in all_branches }

        if self.config.getboolean("clean", fallback=True):
            self.clean()

        for branch in self.branches.values():
            if not branch.dir.is_dir():
                branch.create()
            branch.read_metadata()

        pr_map = self.list_pr_branches()
        for name, branch in self.branches.items():
            if name in pr_map:
                branch.config["pr"] = pr_map[name]
            else:
                branch.config.pop("pr", None)
            branch.save_metadata()

        self.assign_badges()

    def clean(self) -> None:
        expected_files = self.ignored_files.union(*[
            {b.dir, b.lastcommit} for b in self.branches.values()
        ])
        self.runner.log(1, "Cleaning unnecessary files")
        for fn in self.dir.iterdir():
            if fn not in expected_files:
                self.runner.log(2, f"Deleting unknown file {fn}")
                if fn.is_dir():
                    # Dry runs should still delete obsolete branch directories so
                    # that the checkout stays in sync with Github.
                    shutil.rmtree(str(fn))
                elif not self.runner.dryrun:
                    fn.unlink()
        self.runner.exec(2, ["git", "-C", self.checkout, "worktree", "prune"])

    def read(self) -> None:
        self.branches = {}
        if self.dir.is_dir():
            for fn in self.dir.iterdir():
                if not fn.is_dir(): continue
                if fn in self.ignored_files: continue
                name = Branch.parse_filename(fn.name)
                branch = Branch(self, name)
                branch.read_metadata()
                self.branches[name] = branch
            self.assign_badges()

    def assign_badges(self) -> None:
        for field in REPO_BADGES:
            for branch_name in self.config.get(field, "").split():
                if branch_name in self.branches:
                    branch = self.branches[branch_name]
                    branch.badges.append(field)

        main_branch = self.config.get("main", "main")
        if main_branch in self.branches:
            self.branches[main_branch].badges.append("main")

        # Mark branches that correspond to open pull requests
        for branch in self.branches.values():
            pr = branch.config.get("pr")
            if pr:
                branch.badges.append(f"pr#{pr}")

        # Mark branches that are currently queued in slurm
        try:
            result = subprocess.run(
                ["squeue", "--noheader", "--Format=Name:500"],
                capture_output=True,
                text=True,
                check=True,
            )
        except FileNotFoundError:
            self.runner.log(2, "SLURM squeue command not found; skipping queued-branch detection")
            return
        except subprocess.CalledProcessError as e:
            self.runner.log(2, f"squeue failed with exit code {e.returncode}; skipping queued-branch detection")
            return

        prefix = f"nightly:{self.name}:"
        queued = {
            line.strip().removeprefix(prefix)
            for line in result.stdout.splitlines()
            if line.strip().startswith(prefix)
        }
        for branch in self.branches.values():
            if branch.filename in queued:
                branch.badges.append("queued")

    def plan(self) -> None:
        self.runner.log(1, "Filtering branches " + ", ".join(self.branches))
        self.runnable = [branch for branch in self.branches.values() if branch.plan()]
        for branch in self.branches.values():
            if "always" in branch.badges and branch not in self.runnable:
                self.runner.log(2, f"Adding always run on branch {branch.name}")
                self.runnable.append(branch)
            if "baseline" in branch.badges and self.runnable:
                self.runner.log(2, f"Adding baseline branch {branch.name}")
                if branch in self.runnable:
                    self.runnable.remove(branch)
                self.runnable.insert(0, branch)
            if "never" in branch.badges and branch in self.runnable:
                self.runner.log(2, f"Removing never run on branch {branch.name}")
                self.runnable.remove(branch)
        if self.runnable:
            self.runner.log(1, "Found runnable branches " + ", ".join([b.name for b in self.runnable]))
        else:
            self.runner.log(1, "No runnable branches for " + self.name)

        if self.slack:
            try:
                self.slack.post_warnings()
            except slack.SlackError as e:
                self.runner.log(2, f"Slack error: {e}")

class Branch:
    def __init__(self, repo : Repository, name : str):
        self.repo = repo
        self.name = name
        self.filename = Branch.escape_filename(self.name)
        self.dir = self.repo.dir / self.filename
        self.lastcommit = self.repo.dir / (self.filename + ".json")
        self.badges : List[str] = []
        self.config : Dict[str, Any] = {}
        self.current_commit = ""

        self.report_dir = self.dir / self.repo.report_dir_name if self.repo.report_dir_name else None
        self.image_file = self.report_dir / self.repo.image_file_name if self.report_dir and self.repo.image_file_name else None

        self.slack = repo.slack

    def last_run(self) -> float:
        return float(self.config.get("time", "inf"))

    @staticmethod
    def parse_filename(filename : str) -> str:
        return filename.replace("_2f", "/").replace("_25", "%")

    @staticmethod
    def escape_filename(filename : str) -> str:
        return filename.replace("%", "_25").replace("/", "_2f")

    def create(self) -> None:
        relpath = self.dir.relative_to(self.repo.dir)
        self.repo.runner.exec(2, ["git", "-C", self.repo.checkout, "worktree", "add", ".." / relpath, self.name])

    def read_metadata(self) -> None:
        self.config = {}
        if self.lastcommit.exists():
            with self.lastcommit.open() as f:
                try:
                    self.config = json.load(f)
                except json.JSONDecodeError:
                    self.config = {}

    def save_metadata(self) -> None:
        with self.lastcommit.open("w") as last_commit_fd:
            json.dump(self.config, last_commit_fd)

    def plan(self) -> bool:
        self.current_commit = (
            self.repo.runner.exec(
                2, ["git", "-C", self.dir, "rev-parse", "origin/" + self.name]
            ).stdout.decode("ascii").strip()
        )
        if self.config.get("commit", "") == self.current_commit:
            self.repo.runner.log(2, "Branch " + self.name + " has not changed since last run; skipping")
            return False
        return True

if __name__ == "__main__":
    import sys
    conf_file = sys.argv[1] if len(sys.argv) > 1 else "conf/nightlies.conf"
    nightly_runner = NightlyRunner(conf_file)
    nightly_runner.load()
    nightly_runner.run()
