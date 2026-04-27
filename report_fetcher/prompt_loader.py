# prompt_loader.py
import yaml
from pathlib import Path

def load_yaml(filename: str):
    """Load a YAML file from the same directory."""
    file_path = Path(__file__).parent / filename
    if not file_path.exists():
        return {}
    with open(file_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def get_prompt(prompt_name: str) -> str:
    """Get a specific prompt by name from prompts.yaml."""
    prompts = load_yaml("prompts.yaml")
    return prompts.get(prompt_name, "")

def get_llm_config() -> dict:
    """Get the LLM configuration from config.yaml."""
    return get_config_section("llm_config")

def get_config_section(section_name: str) -> dict:
    """Get a specific configuration section from config.yaml."""
    config = load_yaml("config.yaml")
    return config.get(section_name, {})
