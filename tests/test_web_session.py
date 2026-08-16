from app.interfaces.web.session import TelegramWebSession


def test_session_is_bound_to_account_and_signature() -> None:
    session = TelegramWebSession("test-secret")
    cookie = session.issue(42, now=100)

    assert session.valid(cookie, 42, now=100) is True
    assert session.valid(cookie, 43, now=100) is False
    assert session.valid(f"{cookie[:-1]}0", 42, now=100) is False


def test_session_expires() -> None:
    session = TelegramWebSession("test-secret")
    cookie = session.issue(42, now=100)

    assert session.valid(cookie, 42, now=100 + session.max_age) is False
