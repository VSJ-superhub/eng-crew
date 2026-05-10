import requests
from .base import LLMResult, Provider
class OllamaProvider(Provider):
    def has_credentials(self): return True
    def get_client(self): return requests
    def count_tokens(self, text): return len(text) // 4
    def call(self, model, prompt, **kwargs):
        resp = requests.post("http://localhost:11434/api/chat", json={"model":model, "messages":[{"role":"user","content":prompt}], "stream":False})
        data = resp.json()
        return LLMResult(text=data["message"]["content"], provider="ollama", model=model)
