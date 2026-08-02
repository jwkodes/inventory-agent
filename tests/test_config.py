"""Configuration-template regression tests."""

from pathlib import Path

from inventory_agent.config import Settings


def test_example_environment_file_is_valid() -> None:
    example_environment = Path(__file__).parents[1] / ".env.example"

    settings = Settings(_env_file=example_environment)

    assert settings.openai_embedding_dimensions == 512
