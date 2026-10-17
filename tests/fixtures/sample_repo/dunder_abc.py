"""Dunder and abstract methods are never reported."""
import abc


class Base(abc.ABC):
    @abc.abstractmethod
    def must_implement(self):
        raise NotImplementedError

    def __str__(self):
        return "base"

    def concrete(self):
        return 1
