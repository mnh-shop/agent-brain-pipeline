from collections import defaultdict
import os

VALUE = 42
NAMES = ["a", "b"]

class Outer(BaseException):
    class Inner:
        def __init__(self, name: str) -> None:
            self.name = name

        def method(self) -> str:
            return self.name

    def method(self) -> str:
        def nested() -> str:
            return os.path.join("x", "y")

        return nested()


def helper(value: int) -> int:
    return value + VALUE
