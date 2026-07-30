"""Every module still loads.

Most of this repo only ever executes at 14:40 UTC on a GitHub runner. A typo in
a job that no test imports is invisible until the market slot it was supposed to
run in has passed, and the failure looks like an outage rather than a typo. This
is the cheapest possible guard against that: import everything, once.
"""
import importlib
import pkgutil

import pytest

PACKAGES = ("engine", "jobs", "runner")


def module_names():
    out = []
    for package in PACKAGES:
        pkg = importlib.import_module(package)
        out += [f"{package}.{m.name}" for m in pkgutil.iter_modules(pkg.__path__)]
    return sorted(out)


@pytest.mark.parametrize("name", module_names())
def test_module_imports(name):
    importlib.import_module(name)


def test_the_sweep_actually_covers_the_engine():
    """A guard on the guard: if the discovery above silently found nothing, this
    file would pass while testing air."""
    names = module_names()
    assert len(names) > 20
    for expected in ("engine.core", "engine.gitrepo", "jobs.dispatch",
                     "jobs.doctor", "runner.ops"):
        assert expected in names
