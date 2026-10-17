"""Dotted module-path usage."""
import pkg.mod


def use_it():
    return pkg.mod.public_api()
