from __future__ import annotations

from enum import Enum


class Partition(str, Enum):
    TRAIN = "train"
    VALIDATION = "validation"
    TEST = "test"
    OUT_OF_FOLD = "oof"
    SEALED = "sealed"

    @classmethod
    def parse(cls, value: str) -> "Partition":
        return cls(value)
