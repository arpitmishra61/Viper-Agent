import os
from pathlib import Path
from openai import OpenAI

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

PROVIDERS = {
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "api_key": "ollama",
        "default_model": "llama3.2",
        "description": "Local Ollama instance (no API key needed)",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "default_model": "openrouter/free",
        "description": "OpenRouter — free + paid models from many providers",
    },
    "huggingface": {
        "base_url": "https://router.huggingface.co/hf-inference/v1",
        "api_key_env": "HF_TOKEN",
        "default_model": "Qwen/Qwen2.5-7B-Instruct",
        "description": "Hugging Face Inference API (via router)",
    },
    "anthropic": {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "default_model": "anthropic/claude-sonnet-5-5",
        "description": "Claude via OpenRouter (requires OPENROUTER_API_KEY)",
    },
}


def get_client(provider_name: str, api_key_override: str = None) -> tuple:
    if provider_name not in PROVIDERS:
        raise ValueError(
            f"Unknown provider '{provider_name}'. Choose from: {', '.join(PROVIDERS)}"
        )

    config = PROVIDERS[provider_name]

    if api_key_override:
        api_key = api_key_override
    elif "api_key" in config:
        api_key = config["api_key"]
    else:
        env_var = config["api_key_env"]
        api_key = os.environ.get(env_var)
        if not api_key:
            raise ValueError(
                f"API key not found. Set the {env_var} environment variable:\n"
                f"  Windows: $env:{env_var} = 'your-key'\n"
                f"  Or pass it with: --api-key YOUR_KEY"
            )

    client = OpenAI(base_url=config["base_url"], api_key=api_key)
    return client, config
