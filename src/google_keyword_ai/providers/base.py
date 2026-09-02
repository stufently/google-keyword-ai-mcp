from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel


class ProviderInfo(BaseModel):
    name: str
    official: bool
    stability: Literal["stable", "unofficial"]


class Provider(ABC):
    @property
    @abstractmethod
    def info(self) -> ProviderInfo: ...

    @abstractmethod
    def is_available(self) -> bool: ...
