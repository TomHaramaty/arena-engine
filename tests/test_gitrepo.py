"""Writing to a branch that moves under you.

Driven against real git repositories, because the bug is a real git behaviour:
a push rejected as non-fast-forward. Four workflows write the record from four
clones, and `tick` is in its own concurrency group — so it can be pushing a
journal while the daily bell pushes another. Under the old bare `git push`, the
loser of that race raised AFTER its agent's operations were already committed to
Postgres: the trade was real and its explanation never reached the record.
"""
import subprocess

import pytest

from engine import gitrepo


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], check=check,
                          capture_output=True, text=True)


@pytest.fixture
def arena(tmp_path):
    """A bare 'record' and two clones of it, as CI has: one per workflow."""
    remote = tmp_path / "record.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)],
                   check=True)
    clones = []
    for name in ("tick", "bell"):
        path = tmp_path / name
        subprocess.run(["git", "clone", "-q", str(remote), str(path)], check=True)
        git(path, "config", "user.name", "arena-bot")
        git(path, "config", "user.email", "arena-bot@example.com")
        clones.append(path)
    # a first commit so both clones share a base
    (clones[0] / "README.md").write_text("the record\n")
    gitrepo.commit_and_push(clones[0], [clones[0] / "README.md"], "base")
    git(clones[1], "pull", "-q")
    return clones


def journal(repo, agent, text="entry\n"):
    path = repo / "agents" / agent / "journal" / "2026-07-30.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_nothing_to_commit_is_not_an_error(arena):
    tick, _ = arena
    assert gitrepo.commit_and_push(tick, [tick / "README.md"], "again") is False


def test_no_paths_is_not_an_error(arena):
    tick, _ = arena
    assert gitrepo.commit_and_push(tick, [], "nothing") is False


def test_a_losing_push_rebases_and_both_entries_survive(arena, capsys):
    """The race, exactly: the other workflow got there first. Both journals must
    end up in the record — the loser must not lose its entry."""
    tick, bell = arena
    gitrepo.commit_and_push(bell, [journal(bell, "vertex")], "journal(vertex)")

    # tick has been thinking; its clone is now behind by one commit
    assert gitrepo.commit_and_push(
        tick, [journal(tick, "tempo")], "journal(tempo)", sleep=0) is True
    # the retry path really ran — under a bare push this is where the old code
    # raised, with the operations already committed to Postgres
    assert "push rejected" in capsys.readouterr().out

    git(tick, "fetch", "-q")
    published = git(tick, "ls-tree", "-r", "--name-only", "origin/main").stdout
    assert "agents/tempo/journal/2026-07-30.md" in published
    assert "agents/vertex/journal/2026-07-30.md" in published


def test_the_rebase_survives_several_lost_races(arena):
    """A dispatch of nineteen agents against a tick that keeps landing: every
    entry still reaches the record."""
    tick, bell = arena
    for i in range(3):
        gitrepo.commit_and_push(bell, [journal(bell, f"house{i}")], f"tick {i}")
        gitrepo.commit_and_push(tick, [journal(tick, f"seated{i}")], f"bell {i}",
                                sleep=0)
        git(bell, "pull", "-q", "--rebase")
    git(tick, "fetch", "-q")
    published = git(tick, "ls-tree", "-r", "--name-only", "origin/main").stdout
    for i in range(3):
        assert f"agents/house{i}/" in published
        assert f"agents/seated{i}/" in published


def test_an_unpushable_record_raises_rather_than_reporting_success(arena, tmp_path):
    """A caller must be able to tell that the record was not written — silence
    here is what loses an entry."""
    tick, _ = arena
    git(tick, "remote", "set-url", "origin", str(tmp_path / "nowhere.git"))
    with pytest.raises(gitrepo.PushError):
        gitrepo.commit_and_push(tick, [journal(tick, "tempo")], "journal(tempo)",
                                attempts=2, sleep=0)


def test_a_real_conflict_is_raised_not_guessed_at(arena):
    """Journals never collide — but if two writers ever do touch the same lines,
    the engine must refuse to invent a resolution."""
    tick, bell = arena
    gitrepo.commit_and_push(bell, [journal(bell, "tempo", "bell's words\n")],
                            "journal(tempo) from the bell")
    with pytest.raises(gitrepo.PushError):
        gitrepo.commit_and_push(tick, [journal(tick, "tempo", "tick's words\n")],
                                "journal(tempo) from the tick", attempts=2, sleep=0)
    # and the working tree is left in a state a human can read, not mid-rebase
    assert "rebase" not in git(tick, "status").stdout.lower()


def test_a_dirty_tree_is_not_eaten_by_the_rebase(arena):
    """A job part-way through writing several agents' prose has unstaged work.
    Losing a race must not lose it."""
    tick, bell = arena
    gitrepo.commit_and_push(bell, [journal(bell, "vertex")], "journal(vertex)")
    scratch = journal(tick, "rapid", "half-written\n")     # never staged
    gitrepo.commit_and_push(tick, [journal(tick, "tempo")], "journal(tempo)",
                            sleep=0)
    assert scratch.read_text() == "half-written\n"
