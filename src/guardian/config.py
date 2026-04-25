from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from functools import lru_cache


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )

    # LLM
    groq_api_key: str = Field(..., description="Groq API key")

    # Observability
    langsmith_api_key: str = Field(..., description="LangSmith API key")
    langchain_tracing_v2: bool = True
    langchain_project: str = "guardian-health"

    # PostgreSQL
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "guardian_health"
    postgres_user: str = "guardian_health"
    postgres_password: str = Field(..., description="PostgreSQL password")
    database_url: str = Field(..., description="Full PostgreSQL connection URL")

    # Neo4j
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_username: str = "neo4j"
    neo4j_password: str = Field(..., description="Neo4j password")

    # Qdrant
    qdrant_host: str = "localhost"
    qdrant_port: int = 6333

    # Pinecone
    pinecone_api_key: str = Field(..., description="Pinecone API key")
    pinecone_env: str = "us-east-1"

    # HuggingFace
    hf_token: str = Field(..., description="HuggingFace token")

    # Vector store selector
    vector_store: str = "qdrant"

    # FastAPI
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # Guardian specific
    guardian_env: str = "development"
    audit_trail_enabled: bool = True
    hitl_enabled: bool = True


@lru_cache
def get_settings() -> Settings:
    """Returns a cached singleton of Settings.
    Use this everywhere instead of instantiating Settings() directly.
    """
    return Settings()
