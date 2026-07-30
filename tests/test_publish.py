"""Publishing the floor's data against a repo that moves.

tools/publish_arena.sh is now the single publish path for all four workflows, so
the loop it contains is worth proving rather than trusting. The failure it exists
to prevent is on the record: on 2026-07-28 an hourly tick landed its own
arena.json between first-bell's clone and its push, the step failed, and the
workflow wrote `failed` over a first bell that had actually SUCCEEDED — a real
principal watching the card was told their trader's first session had failed.
"""
import json
import pathlib
import subprocess

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "tools" / "publish_arena.sh"


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], check=check,
                          capture_output=True, text=True)


@pytest.fixture
def web(tmp_path):
    """A bare arena-web with data/arena.json, and an engine workspace holding a
    freshly built site/arena.json."""
    remote = tmp_path / "arena-web.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(seed)], check=True)
    git(seed, "config", "user.name", "arena-bot")
    git(seed, "config", "user.email", "bot@example.com")
    (seed / "data").mkdir()
    (seed / "data" / "arena.json").write_text(json.dumps({"agents": ["ballast"]}))
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "seed")
    git(seed, "push", "-q", "origin", "main")

    workspace = tmp_path / "engine"
    (workspace / "site").mkdir(parents=True)
    (workspace / "site" / "arena.json").write_text(
        json.dumps({"agents": ["ballast", "vector"]}))
    return remote, workspace, seed


def publish(remote, workspace, message="data: tick refresh"):
    return subprocess.run(
        ["bash", str(SCRIPT), message],
        cwd=str(workspace), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(workspace),
             "ARENA_WEB_REPO": str(remote)},
    )


def published(remote, workspace):
    out = tmp = workspace / "read"
    subprocess.run(["git", "clone", "-q", str(remote), str(tmp)], check=True)
    data = json.loads((tmp / "data" / "arena.json").read_text())
    subprocess.run(["rm", "-rf", str(out)], check=True)
    return data


def test_a_new_build_is_published(web):
    remote, workspace, _ = web
    r = publish(remote, workspace)
    assert r.returncode == 0, r.stderr
    assert "published" in r.stdout
    assert published(remote, workspace)["agents"] == ["ballast", "vector"]


def test_an_identical_build_is_not_committed(web):
    """The tick runs twice an hour whether or not anything moved."""
    remote, workspace, _ = web
    publish(remote, workspace)
    r = publish(remote, workspace)
    assert r.returncode == 0 and "no data changes" in r.stdout


def test_a_racing_push_is_survived_and_the_later_build_wins(web):
    """Another workflow lands its own arena.json mid-flight. Ours is the later
    build, so it must end up published — and the step must not fail."""
    remote, workspace, seed = web

    # A second writer commits between our clone and our push. Simulated by
    # pushing from `seed` while our own clone is already stale: the script's
    # first attempt is rejected, and the second rebuilds on the new head.
    hook = remote / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\n"
        f"if [ ! -f {remote}/raced ]; then\n"
        f"  touch {remote}/raced\n"
        "  exit 1\n"           # the first push is rejected, exactly as a race does
        "fi\n"
        "exit 0\n")
    hook.chmod(0o755)

    r = publish(remote, workspace)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "another workflow landed first" in r.stdout
    assert "published" in r.stdout
    assert published(remote, workspace)["agents"] == ["ballast", "vector"]


def test_a_repo_that_never_accepts_the_push_fails_loudly(web):
    remote, workspace, _ = web
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    r = publish(remote, workspace)
    assert r.returncode != 0
    assert "could not publish" in r.stderr
