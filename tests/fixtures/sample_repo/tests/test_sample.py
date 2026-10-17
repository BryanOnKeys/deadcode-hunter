"""Test-file handling."""
import pytest

from alive import used_alive


@pytest.fixture
def db():
    return {}


def test_addition(db):
    assert used_alive() == 42
    helper()


def helper():
    return "help"


def unused_helper():
    return "unused"
