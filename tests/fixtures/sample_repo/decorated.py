"""Decorated symbols get medium confidence."""

ROUTES = {}


def route(path):
    def deco(func):
        ROUTES[path] = func
        return func

    return deco


@route("/index")
def index():
    return "index"


class Page:
    @property
    def title(self):
        return "title"
