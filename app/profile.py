"""Per-client business configuration, loaded from profile.yaml.

Everything that makes one client different from another lives in one file:
who the company is and what it does (sent to Gemini when scoring), which
keywords and countries the collectors search for, how relevance is judged, and
the alert threshold. Secrets and infrastructure (API keys, SMTP, database)
stay in the environment (.env); business choices live here.

The file is validated when the app starts. A typo, a missing section or an
invalid regex stops startup with a message naming the problem, instead of
failing halfway through a collection run.

Where the file is read from: PROFILE_PATH (env) if set, otherwise
profile.yaml in the project root. profile.example.yaml documents every field.
"""

import re
from pathlib import Path
from typing import Any, Dict, List, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.config import settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE_PATH = PROJECT_ROOT / "profile.yaml"


class ProfileError(Exception):
    """profile.yaml is missing, unreadable or invalid."""


class _Strict(BaseModel):
    # Unknown keys are errors, so a typo like "serch:" cannot silently do nothing.
    model_config = ConfigDict(extra="forbid")


class SearchConfig(_Strict):
    terms: List[str] = Field(min_length=1)
    relevance_patterns: List[str] = Field(min_length=1)
    target_countries: List[str] = Field(min_length=1)
    worldbank_country_values: List[str] = Field(min_length=1)
    min_days_to_deadline: int = Field(default=5, ge=0, le=365)

    @field_validator("terms", "relevance_patterns", "target_countries", "worldbank_country_values")
    @classmethod
    def _entries_not_blank(cls, values: List[str]) -> List[str]:
        if any(not value.strip() for value in values):
            raise ValueError("blank entries are not allowed")
        return values

    @field_validator("relevance_patterns")
    @classmethod
    def _patterns_compile(cls, values: List[str]) -> List[str]:
        for pattern in values:
            try:
                re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"invalid regex {pattern!r}: {exc}") from exc
        return values

    def compiled_patterns(self) -> List[re.Pattern]:
        return [re.compile(pattern, re.IGNORECASE) for pattern in self.relevance_patterns]


class AlertsConfig(_Strict):
    min_score: float = Field(default=6.5, ge=0, le=10)


class TenderProfile(_Strict):
    company_name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    core_services: List[str] = Field(min_length=1)
    target_geographies: List[str] = Field(min_length=1)
    search: SearchConfig
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)

    def company_dict(self) -> Dict[str, Any]:
        """The shape the Gemini scoring prompt expects."""
        return {
            "company_name": self.company_name,
            "description": self.description,
            "core_services": list(self.core_services),
            "target_geographies": list(self.target_geographies),
        }


def load_profile(path: Union[str, Path, None] = None) -> TenderProfile:
    if path:
        chosen = Path(path)
    elif settings.PROFILE_PATH:
        chosen = Path(settings.PROFILE_PATH)
    else:
        chosen = DEFAULT_PROFILE_PATH
    if not chosen.is_absolute():
        chosen = PROJECT_ROOT / chosen

    if not chosen.is_file():
        raise ProfileError(
            f"Profile file not found: {chosen}. Copy profile.example.yaml to "
            "profile.yaml and edit it, or set PROFILE_PATH."
        )
    try:
        data = yaml.safe_load(chosen.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ProfileError(f"Could not read {chosen}: {exc}") from exc
    if not isinstance(data, dict):
        raise ProfileError(f"{chosen} must contain a mapping of settings at the top level.")
    try:
        return TenderProfile.model_validate(data)
    except ValidationError as exc:
        raise ProfileError(f"{chosen} is invalid:\n{exc}") from exc


profile: TenderProfile = load_profile()


def alert_min_score() -> float:
    """Alert threshold: ALERT_MIN_SCORE from the environment wins if set,
    otherwise the profile's alerts.min_score."""
    if settings.ALERT_MIN_SCORE is not None:
        return settings.ALERT_MIN_SCORE
    return profile.alerts.min_score
