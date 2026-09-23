from types import SimpleNamespace

from truffle import config
from truffle.db import add_usage, month_key, usage_this_month
from truffle.pipeline import CG_HOST, Pipeline, _next_month_start


def pipeline(tmp_path, ignore_budget=False):
    cfg = config.load()
    cfg["db_path"], cfg["cache_dir"] = tmp_path / "t.db", tmp_path / "cache"
    args = SimpleNamespace(refresh=True, ignore_budget=ignore_budget, dry_run=True,
                           skip_metadata=True, limit=None, date=None)
    return Pipeline(cfg, args)


def test_usage_accumulates_per_month(tmp_path):
    p = pipeline(tmp_path)
    assert usage_this_month(p.con, CG_HOST) == 0
    add_usage(p.con, CG_HOST, 40)
    add_usage(p.con, CG_HOST, 2)
    assert usage_this_month(p.con, CG_HOST) == 42


def test_record_usage_persists_http_counts(tmp_path):
    p = pipeline(tmp_path)
    p.http.per_host[CG_HOST] = 17
    p.http.per_host["api.github.com"] = 5
    p.record_usage()
    assert usage_this_month(p.con, CG_HOST) == 17
    assert usage_this_month(p.con, "api.github.com") == 5


def test_budget_allows_a_run_that_fits(tmp_path):
    p = pipeline(tmp_path)
    assert p.cg_projection(300) < p.cfg["http"]["monthly_budget"][CG_HOST]
    assert p.check_budget(300) is True


def test_budget_aborts_when_the_projection_exceeds_what_is_left(tmp_path):
    p = pipeline(tmp_path)
    budget = p.cfg["http"]["monthly_budget"][CG_HOST]
    add_usage(p.con, CG_HOST, budget - 10)
    assert p.check_budget(300) is False


def test_ignore_budget_overrides_the_abort(tmp_path):
    p = pipeline(tmp_path, ignore_budget=True)
    add_usage(p.con, CG_HOST, p.cfg["http"]["monthly_budget"][CG_HOST])
    assert p.check_budget(300) is True


def test_no_budget_configured_is_always_allowed(tmp_path):
    p = pipeline(tmp_path)
    p.cfg["http"]["monthly_budget"] = {}
    assert p.check_budget(10_000) is True


def test_month_key_and_reset_date(tmp_path):
    from datetime import datetime, timezone
    dec = datetime(2026, 12, 9, tzinfo=timezone.utc)
    assert month_key(dec) == "2026-12" and _next_month_start(dec) == "2027-01-01"
