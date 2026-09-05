"""Runtime configuration."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


def default_home() -> Path:
    return Path(os.environ.get("NC_HOME", Path.home() / ".neocortex"))


@dataclass
class Config:
    home: Path = field(default_factory=default_home)
    adapter: str = "codex"
    models: dict[str, str] = field(default_factory=lambda: {
        "worker": "gpt-6-astra",
        "critic": "gpt-6-astra",
    })
    turn_timeout_s: int = 900
    preflight_timeout_s: int = 120
    max_consecutive_failures: int = 3
    min_free_mb: int = 120
    max_turns_per_cycle: int = 0          # 0 = unlimited, bounded by budget/tasks
    ask_timeout_s: int = 24 * 3600

    @property
    def db_path(self) -> Path:
        return self.home / "state.db"

    @property
    def runs_dir(self) -> Path:
        return self.home / "runs"

    @property
    def work_dir(self) -> Path:
        return self.home / "work"

    @property
    def config_path(self) -> Path:
        return self.home / "config.json"

    @classmethod
    def load(cls, home: Path | None = None) -> Config:
        home = Path(home) if home else default_home()
        cfg = cls(home=home)
        path = cfg.config_path
        if path.exists():
            data = json.loads(path.read_text())
            data.pop("home", None)
            for key, value in data.items():
                if hasattr(cfg, key):
                    setattr(cfg, key, value)
        return cfg

    def save(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        data = {k: v for k, v in asdict(self).items() if k != "home"}
        self.config_path.write_text(json.dumps(data, indent=2))

    def model_for(self, role: str) -> str:
        return self.models.get(role, next(iter(self.models.values())))
