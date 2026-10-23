"""Small explicit state set for workflow jobs."""

from enum import Enum


class JobState(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
