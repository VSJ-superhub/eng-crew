import requests
from .base import LLMResult, Provider

OLLAMA_URL = "http://localhost:11434"


def is_available(timeout: float = 0.5) -> bool:
    """Is a local Ollama daemon reachable?

    The dashboard's stack picker calls this to decide whether to offer the
    local-model stacks. The timeout is deliberately short: when Ollama is not
    running — the common case — the page must not stall waiting for it.
    """
    try:
        requests.get(f"{OLLAMA_URL}/api/tags", timeout=timeout)
        return True
    except Exception:
        return False


class OllamaProvider(Provider):
    def has_credentials(self): return True
    def get_client(self): return requests
    def count_tokens(self, text): return len(text) // 4
    def call(self, model, prompt, **kwargs):
        resp = requests.post(f"{OLLAMA_URL}/api/chat", json={"model":model, "messages":[{"role":"user","content":prompt}], "stream":False})
        data = resp.json()
        return LLMResult(text=data["message"]["content"], provider="ollama", model=model)
