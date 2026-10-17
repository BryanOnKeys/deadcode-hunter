"""Instance-method resolution samples."""


class Worker:
    def live_method(self):
        return self._helper()

    def _helper(self):
        return 1

    def dead_method(self):
        return 2
