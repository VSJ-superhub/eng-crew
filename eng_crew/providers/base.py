from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional, Protocol, Tuple, Union
class QuotaExceeded(Exception): pass
class ProviderUnavailable(Exception): pass
COST_RATES = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "gemini-2.0-flash": (0.075, 0.3),
}
@dataclass
class LLMResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    provider: str = ""
    model: str = ""
    session_id: str = ""   # CLI session, for --resume continuity
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
