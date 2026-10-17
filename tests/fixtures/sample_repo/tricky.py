"""Tricky scoping cases."""


def recursive(n):
    if n <= 0:
        return 0
    return recursive(n - 1)


def outer():
    def inner():
        return 1

    return inner()


def outer2():
    def inner2():
        return 2

    return inner2()


def outer3():
    def inner3():
        return 3

    return 4
