from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping


@dataclass(slots=True)
class WorkerError(Exception):
    code: str
    message: str
    details: Mapping[str, Any]

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)
        self.details = MappingProxyType(dict(self.details))

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": dict(self.details),
            },
        }
