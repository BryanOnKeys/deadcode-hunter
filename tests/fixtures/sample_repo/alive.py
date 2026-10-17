"""Symbols that are provably alive."""

__all__ = ["exported_thing", "PUBLIC_CONST"]

PUBLIC_CONST = 1


def used_alive():
    return 42


def exported_thing():
    return "exported"


def _private_unused():
    return "dead"
