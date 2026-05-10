from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional, Protocol, Tuple, Union
class QuotaExceeded(Exception): pass
class ProviderUnavailable(Exception): pass
COST_RATES = {"claude-sonnet-4-6": (3.0, 15.0), "gemini-2.0-flash": (0.075, 0.3)}
@dataclass
class LLMResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    provider: str = ""
    model: str = ""
class Provider(ABC):
    @abstractmethod
    def has_credentials(self): pass
    @abstractmethod
    def get_client(self): pass
    @abstractmethod
    def count_tokens(self, text): pass
    @abstractmethod
    def call(self, model, prompt, **kwargs): pass
def calculate_cost(model, in_t, out_t):
    rates = COST_RATES.get(model, (0.0, 0.0))
    return (in_t * rates[0] + out_t * rates[1]) / 1000000
