from pathlib import Path
import os
import tomllib

from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent


class DeepSeekConfig(BaseModel):
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    timeout_seconds: int = Field(default=25, ge=1, le=120)


class AppConfig(BaseModel):
    title: str = "补货建议与库存检查"
    data_dir: str = "data"
    database_url: str = ""
    redis_url: str = ""
    cache_ttl_seconds: int = Field(default=120, ge=0)
    node_timeout_seconds: int = Field(default=45, ge=1)
    max_tool_calls: int = Field(default=8, ge=4, le=30)
    max_graph_steps: int = Field(default=16, ge=10, le=60)
    seed_demo: bool = True


class Settings(BaseModel):
    deepseek: DeepSeekConfig = Field(default_factory=DeepSeekConfig)
    app: AppConfig = Field(default_factory=AppConfig)

    @property
    def data_path(self) -> Path:
        p = Path(self.app.data_dir)
        return p if p.is_absolute() else ROOT / p


def load_settings() -> Settings:
    path = Path(os.environ.get("RETAIL_CONFIG", str(ROOT / "config.local.toml")))
    if not path.exists():
        path = ROOT / "config.example.toml"
    with path.open("rb") as f:
        settings = Settings.model_validate(tomllib.load(f))
    # 环境变量适用于容器和给不同用户独立部署；优先于文件。
    for env, field in [("DEEPSEEK_API_KEY", "api_key"), ("DEEPSEEK_BASE_URL", "base_url"), ("DEEPSEEK_MODEL", "model")]:
        if env in os.environ:
            setattr(settings.deepseek, field, os.environ[env])
    for env, field in [("DATABASE_URL", "database_url"), ("REDIS_URL", "redis_url")]:
        if os.environ.get(env):
            setattr(settings.app, field, os.environ[env])
    return settings
