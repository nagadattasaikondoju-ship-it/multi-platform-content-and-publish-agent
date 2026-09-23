from grow_it.llm import retry_delay


def test_retry_delay_uses_server_hint():
    assert retry_delay(Exception("Please retry in 9.6s."), 0) == 10.6


def test_retry_delay_backs_off_and_caps():
    assert 4 <= retry_delay(Exception("busy"), 2) < 5
    assert retry_delay(Exception("busy"), 10) < 31


def test_long_quota_delay_moves_on_quickly():
    assert retry_delay(Exception("Please retry in 41234.5s."), 0) == 1.0
