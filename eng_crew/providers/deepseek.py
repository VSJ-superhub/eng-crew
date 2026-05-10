import os, requests
from .base import LLMResult, Provider
class DeepSeekProvider(Provider):
    def has_credentials(self): return bool(os.environ.get("DEEPSEEK_API_KEY"))
    def get_client(self): return requests
    def count_tokens(self, text): return len(text) // 4
    def call(self, model, prompt, **kwargs):
        headers = {"Authorization": f"Bearer {os.environ.get('DEEPSEEK_API_KEY')}"}
        resp = requests.post("https://api.deepseek.com/chat/completions", headers=headers, json={"model":model, "messages":[{"role":"user","content":prompt}]})
        data = resp.json()
        return LLMResult(text=data["choices"][0]["message"]["content"], provider="deepseek", model=model)
