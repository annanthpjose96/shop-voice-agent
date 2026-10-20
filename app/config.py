from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Any OpenAI-compatible provider. Defaults to Groq (free tier, no card needed).
    llm_api_key: str = ""
    llm_base_url: str = "https://api.groq.com/openai/v1"
    model: str = "llama-3.3-70b-versatile"

    twilio_auth_token: str = ""
    public_host: str = "localhost:8000"
    validate_twilio_signature: bool = True
    enable_test_ui: bool = False  # browser voice test at /test (development only)
    human_transfer_number: str = ""

    tts_provider: str = "Google"
    tts_voice: str = "en-US-Journey-O"
    stt_provider: str = "Deepgram"

    database_path: str = "data/shop.db"
    business_config: str = "business.json"

    max_tool_rounds: int = 5
    end_delay_seconds: float = 4.0  # lets the goodbye finish before Twilio hangs up/transfers
