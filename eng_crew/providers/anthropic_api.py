import os
from .base import LLMResult, Provider, calculate_cost
class AnthropicProvider(Provider):
    def has_credentials(self): return bool(os.environ.get("ANTHROPIC_API_KEY"))
    def get_client(self):
        import anthropic
        return anthropic.Anthropic()
    def count_tokens(self, text): return len(text) // 4
    def call(self, model, prompt, **kwargs):
        client = self.get_client()
        resp = client.messages.create(model=model, max_tokens=8192, messages=[{"role":"user","content":prompt}])
        return LLMResult(text=resp.content[0].text, input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens, provider="anthropic", model=model)
