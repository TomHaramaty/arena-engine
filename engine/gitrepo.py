"""Writing to the record: commit and push, against a branch that moves.

The record lives in a git repo that four workflows push to (tick, daily-run,
reflect, first-bell) from four separate clones, and `tick` runs in its own
concurrency group — so it can be mid-flight while the daily bell is running.
Every writer used to end with a bare `git push` under `check=True`.

What that costs is specific and severe. jobs/agent_run applies an agent's
operations to Postgres and commits them, THEN writes and pushes the journal. A
push rejected as non-fast-forward raises there, after the book has moved: the
fills are real, the run row stays 'started' forever, and the entry explaining
the trade never reaches the record. The dispatcher deliberately never re-fires a
crashed run, so nothing would ever write it. That is precisely the divergence
between the book and the record this repo exists to prevent.

So: one writer, used by every job. Rebase onto whatever landed while we were
thinking, and push again. A journal or a reflection is a new file or an append to
a file only its own agent writes, so there is nothing for a rebase to conflict
with; if one somehow does, the rebase is aborted and the failure is raised
loudly rather than resolved by guesswork.
"""
import subprocess
import time


class PushError(Exception):
    pass


def _git(repo, *args, check=True, capture=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=capture,
        text=True,
    )


def commit_and_push(repo, paths, message, attempts=4, sleep=2.0):
    """Stage `paths`, commit as `message`, and push — rebasing on rejection.

    Returns True if something was committed, False if there was nothing to
    commit (a resumed job re-writing identical prose). Raises PushError if the
    record could not be written after `attempts`, so the caller can record the
    failure honestly instead of a silent success.
    """
    repo = str(repo)
    paths = [str(p) for p in (paths or [])]
    if not paths:
        return False
    _git(repo, "add", "--", *paths)
    if _git(repo, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return False
    _git(repo, "commit", "-q", "-m", message)

    last = ""
    for attempt in range(attempts):
        push = _git(repo, "push", "-q", check=False)
        if push.returncode == 0:
            return True
        last = (push.stderr or push.stdout or "").strip()[:500]
        if attempt == attempts - 1:
            break
        print(f"  push rejected ({last.splitlines()[-1] if last else '?'}) — "
              f"rebasing onto the branch as it stands, attempt {attempt + 1}")
        # --autostash: a job that writes several agents' prose may have a dirty
        # tree from work not yet staged for ITS commit; rebasing must not eat it.
        pull = _git(repo, "pull", "--rebase", "--autostash", "-q", check=False)
        if pull.returncode != 0:
            _git(repo, "rebase", "--abort", check=False)
            raise PushError(
                "could not rebase onto the record — refusing to guess at a "
                f"resolution: {(pull.stderr or '').strip()[:500]}"
            )
        time.sleep(sleep)
    raise PushError(f"could not push to the record after {attempts} attempts: {last}")
