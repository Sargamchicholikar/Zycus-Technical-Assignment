"""LLM backend shared by llm_explainer.py and presentation/generator.py.

A fully local model served by LM Studio's OpenAI-compatible API — no
cloud dependency, no API key, no rate limits. This exists as its own
module so both call sites share one client instead of duplicating it.
"""

import os

TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.2"))
MAX_TOKENS = 800


def generate(system_instruction: str, user_prompt: str) -> str:
    """Returns the raw text response from the local LM Studio model."""
    from openai import OpenAI

    base_url = os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234/v1")
    model = os.environ.get("LMSTUDIO_MODEL", "phi-3-mini-4k-instruct")
    client = OpenAI(base_url=base_url, api_key="lm-studio")
    try:
        response = client.chat.completions.create(
            model=model,
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_prompt},
            ],
        )
    except Exception as exc:
        raise RuntimeError(
            f"Could not reach LM Studio's local server at {base_url}. Make sure LM Studio is "
            "running with a model loaded (`lms load <model>`) and its local server started "
            "(`lms server start`)."
        ) from exc
    return response.choices[0].message.content or ""
