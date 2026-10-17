"""Cross-module definitions."""


def public_api():
    return "api"


def internal_dead():
    return "dead"


class Greeter:
    def greet(self):
        return self._format()

    def _format(self):
        return "hi"


def unused_top():
    return 0
