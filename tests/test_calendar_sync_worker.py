from app.services import calendar_sync


def test_without_failures_the_worker_sleeps_until_woken():
    assert calendar_sync.next_wait_timeout(0) is None


def test_with_failures_the_worker_retries_every_15_minutes():
    assert calendar_sync.next_wait_timeout(2) == 900


def test_a_crashing_pass_counts_as_a_failure_so_it_is_retried():
    def broken_session_factory():
        raise RuntimeError("db down")

    assert calendar_sync.sync_pass(broken_session_factory, client=None) == 1


def test_request_calendar_sync_sets_the_signal():
    calendar_sync._wake.clear()
    calendar_sync.request_calendar_sync()
    assert calendar_sync._wake.is_set()
    calendar_sync._wake.clear()
