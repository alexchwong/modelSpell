"""ModelSpell basics. Usage: python examples/basic.py <lmstudio-model-key>  (needs LM Studio running)."""
import os
import sys

from modelspell import Endpoint, complete, list_models

LOCAL = Endpoint(kind="lmstudio")  # http://localhost:1234/v1
CLOUD = Endpoint(kind="openrouter", api_key=os.environ.get("OPENROUTER_API_KEY"))  # key held in memory only

for m in list_models(LOCAL):
    print(f"{m.id:40} loaded={m.loaded} reasoning={m.reasoning_options}")

model = sys.argv[1] if len(sys.argv) > 1 else "qwen/qwen3-8b"

# Streamed call: reasoning is printed live; the Result still carries everything.
print("\n--- reasoning ---")
result = complete(
    LOCAL, model, [{"role": "system", "content": "Answer in one sentence."},
                   {"role": "user", "content": "Why is the sky blue?"}],
    reasoning="low", max_tokens=2048,
    on_reasoning=lambda s: print(s, end="", flush=True),
    on_text=lambda s: None,  # any callback switches streaming on
)
print("\n--- answer ---")
print(result.text)
print("usage:", result.usage, "cost:", result.cost, "truncated:", result.truncated, "via:", result.transport)

# Repair-style follow-up: system, user, assistant, user.
# With reasoning="off" this goes to LM Studio's native route, which flattens the history.
followup = complete(
    LOCAL, model,
    [{"role": "system", "content": "Answer in one sentence."},
     {"role": "user", "content": "Why is the sky blue?"},
     {"role": "assistant", "content": result.text},
     {"role": "user", "content": "Now say it in under ten words."}],
    reasoning="off", max_tokens=256,
)
print("\nfollow-up:", followup.text, f"({followup.transport})")

if CLOUD.api_key:
    r = complete(CLOUD, "openai/gpt-4o-mini", "Say hi.", max_tokens=16)
    print("\nopenrouter:", r.text, "cost:", r.cost, "provider:", r.provider)
