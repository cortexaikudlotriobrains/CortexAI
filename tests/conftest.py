import os

import pytest
from dotenv import load_dotenv

# Load environment variables from .env file for tests
load_dotenv()


@pytest.fixture(autouse=True)
def isolated_pricing_mode(monkeypatch):
    """Unit tests must not inherit an operator's database-pricing rollout flag.

    Tests exercising persisted rates explicitly opt into database mode.
    """
    monkeypatch.setenv("MODEL_PRICING_MODE", "legacy")


@pytest.fixture(scope="session")
def api_key():
    """Fixture to provide the API key from environment variables."""
    key = os.getenv("GOOGLE_API_KEY")
    if not key:
        pytest.skip("GOOGLE_API_KEY environment variable not set")
    return key


@pytest.fixture
def mock_env(monkeypatch):
    """Fixture to mock environment variables for testing."""
    # Mock environment variables
    env_vars = {
        "MODEL_TYPE": "gemini",
        "GOOGLE_API_KEY": "test-api-key",
        "GEMINI_MODEL": "gemini-pro",
    }
    for key, value in env_vars.items():
        monkeypatch.setenv(key, value)
    return env_vars
