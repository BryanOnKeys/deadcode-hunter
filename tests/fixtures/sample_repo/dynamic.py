"""Dynamic constructs lower confidence to 'low'."""


def fetch(key, config):
    return getattr(config, key)


def load(name):
    return eval(name)
