"""Tests for the per-client profile (profile.yaml) and the code that uses it."""

from types import SimpleNamespace

import pytest
import yaml

from app import profile as profile_module
from app.profile import ProfileError, load_profile, profile

MATCHING_TITLES = [
    "ESIA for the Test Corridor Project",
    "Consultancy Service for Conduct Full Socio-Economic Impact Assessments in existing and New MCAs",
    "Final Independent Evaluation of the Extending Social Protection Coverage Programme",
    "Annual Environmental Audit and Safety Inspection for Municipal Facilities",
    "Resettlement Action Plan for the Kisumu Water Project",
    "Feasibility Study for a Regional Wastewater Scheme",
    "Conduct EIA study for the Mombasa road upgrade",
    "Environmental Impact Assessment (EIA) for Solar Plant",
]

IGNORED_TITLES = [
    "Supply of Fuel and Lubricants to the Indonesia Country Office",
    "Maintenance of aircraft stands at EIA terminal",
    "Provision of Catering Services for the Annual Conference",
]


def _write_variant(tmp_path, mutate):
    data = yaml.safe_load(
        (profile_module.PROJECT_ROOT / "profile.yaml").read_text(encoding="utf-8")
    )
    mutate(data)
    path = tmp_path / "variant.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_hueri_profile_loads():
    assert profile.company_name == "HUERI Limited"
    assert "Kenya" in profile.target_geographies
    assert profile.search.min_days_to_deadline == 5
    assert 0 <= profile.alerts.min_score <= 10


@pytest.mark.parametrize("title", MATCHING_TITLES)
def test_relevance_patterns_match_expected_titles(title):
    assert any(p.search(title) for p in profile.search.compiled_patterns())


@pytest.mark.parametrize("title", IGNORED_TITLES)
def test_relevance_patterns_ignore_unrelated_titles(title):
    assert not any(p.search(title) for p in profile.search.compiled_patterns())


def test_collectors_read_the_profile():
    from app.collectors import ungm, worldbank

    expected = [p.pattern for p in profile.search.compiled_patterns()]
    for module in (ungm, worldbank):
        assert module.SEARCH_TERMS == profile.search.terms
        assert [p.pattern for p in module.RELEVANCE_PATTERNS] == expected
        assert module.MIN_DAYS_TO_DEADLINE == profile.search.min_days_to_deadline
    assert ungm.TARGET_COUNTRIES == tuple(c.lower() for c in profile.search.target_countries)
    assert worldbank.TARGET_COUNTRY_VALUES == profile.search.worldbank_country_values


def test_alert_threshold_env_override_wins(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "ALERT_MIN_SCORE", None)
    assert profile_module.alert_min_score() == profile.alerts.min_score
    monkeypatch.setattr(settings, "ALERT_MIN_SCORE", 8.2)
    assert profile_module.alert_min_score() == 8.2


def test_example_profile_is_valid():
    example = load_profile(profile_module.PROJECT_ROOT / "profile.example.yaml")
    assert example.company_name
    assert example.search.compiled_patterns()


def test_missing_profile_file_is_reported(tmp_path):
    with pytest.raises(ProfileError, match="not found"):
        load_profile(tmp_path / "nope.yaml")


def test_unknown_key_is_rejected(tmp_path):
    path = _write_variant(tmp_path, lambda d: d.update({"serch": {}}))
    with pytest.raises(ProfileError, match="serch"):
        load_profile(path)


def test_invalid_regex_is_rejected(tmp_path):
    path = _write_variant(tmp_path, lambda d: d["search"]["relevance_patterns"].append("("))
    with pytest.raises(ProfileError, match="invalid regex"):
        load_profile(path)


def test_missing_section_and_bad_score_are_rejected(tmp_path):
    no_search = _write_variant(tmp_path, lambda d: d.pop("search"))
    with pytest.raises(ProfileError):
        load_profile(no_search)

    def too_high(data):
        data["alerts"]["min_score"] = 11

    with pytest.raises(ProfileError):
        load_profile(_write_variant(tmp_path, too_high))


def test_alert_messages_use_the_company_name():
    from app.services.notifier import notifier

    tender = SimpleNamespace(
        external_id="X-1",
        source="TEST",
        title="ESIA for a corridor",
        buyer="Agency",
        url="https://example.com/x-1",
        deadline_str="2026-12-31",
        analysis=SimpleNamespace(
            relevance_score=7.0, executive_summary="Fit.", eligibility_gaps=[]
        ),
    )
    subject, _ = notifier._render_digest([tender])
    assert profile.company_name in subject
    assert profile.company_name in notifier._render_sms_text([tender])
