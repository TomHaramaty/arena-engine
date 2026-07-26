"""Optional Sentry wiring for the engine jobs.

No-op unless ``SENTRY_DSN`` is set, so local runs, forks, and tests are
unaffected. When active, genuine unhandled exceptions are captured with a
``component`` tag and the commit as release. Scheduled jobs additionally check
in to a Sentry Cron Monitor whose config only raises an issue after two
consecutive bad windows — a real outage — rather than on every self-healing
blip. That matches the over-provisioned ``tick`` schedule: a single missed
attempt is covered by the next one and should never page anybody.

Expected data-fetch misses are *not* bugs: the jobs journal them and skip, and
``SystemExit`` (e.g. tick aborting on no quotes) is ignored by Sentry error
capture — it only shows up as a cron check-in, absorbed by the 2-window
threshold.
"""
import contextlib
import os

_DSN = os.environ.get("SENTRY_DSN")
_ready = False


def init(component):
    """Initialise Sentry for a job, tagged by ``component``. Returns True if active."""
    global _ready
    if not _DSN:
        return False
    try:
        import sentry_sdk
    except ImportError:
        return False
    sentry_sdk.init(
        dsn=_DSN,
        environment=os.environ.get("SENTRY_ENV", "production"),
        release=os.environ.get("GITHUB_SHA") or None,
        traces_sample_rate=0.0,
    )
    sentry_sdk.set_tag("component", component)
    _ready = True
    return True


@contextlib.contextmanager
def cron(monitor_slug, crontab):
    """Wrap a scheduled job in a Sentry Cron check-in.

    Upserts the monitor from code (no dashboard setup) and configures it to
    alert only after two consecutive missed/failed windows, so an
    over-provisioned schedule's self-healing blips stay quiet.
    """
    if not _ready:
        yield
        return
    from sentry_sdk.crons import monitor
    monitor_config = {
        "schedule": {"type": "crontab", "value": crontab},
        "timezone": "UTC",
        "checkin_margin": 10,           # minutes late allowed before "missed"
        "max_runtime": 15,              # minutes before "timed out"
        "failure_issue_threshold": 2,   # two bad windows in a row before an issue
        "recovery_threshold": 1,
    }
    with monitor(monitor_slug=monitor_slug, monitor_config=monitor_config):
        yield
