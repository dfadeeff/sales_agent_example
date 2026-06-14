from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).parent.parent / ".env",
        extra="ignore",
    )

    # Accept both names the .env might use
    anthropic_api_key: str = ""
    claude_api_key: str = ""

    secret_key: str = "change-me-in-production-32-chars-min"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24 * 7  # 1 week

    db_path: str = str(Path(__file__).parent.parent / "Chinook_Sqlite.sqlite")

    @property
    def effective_api_key(self) -> str:
        return self.anthropic_api_key or self.claude_api_key


settings = Settings()
