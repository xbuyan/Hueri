import json
import logging
import random
import re
import threading
import time

from google import genai
from google.genai import errors, types

from app.config import settings
from app.schemas import TenderAIAnalysisResult

logger = logging.getLogger("services.ai_agent")

# Models are tried in order. Each model has its own quota, so when one is
# exhausted the next one takes over.
MODEL_CHAIN = ["gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]

MIN_INTERVAL_SECONDS = 6.0   # pacing between calls (about 10 requests/minute)
MAX_ATTEMPTS_PER_MODEL = 3
TRANSIENT_CODES = {500, 503, 504}
COOLDOWN_SECONDS = 3600      # how long an exhausted or unavailable model is skipped


class TenderAgentError(Exception):
    """Raised when scoring cannot be performed. Never swallowed into a fake score."""


class QuotaExhaustedError(TenderAgentError):
    """Every model in MODEL_CHAIN is out of quota or unavailable."""


class FatalAgentError(TenderAgentError):
    """The API key was rejected: every tender would fail the same way, so abort the run."""


def _is_daily_quota(exc) -> bool:
    return "perday" in str(exc).lower()


def _suggested_delay(exc, default: float) -> float:
    match = re.search(r"retry in ([\d.]+)s", str(exc))
    return float(match.group(1)) + 1.0 if match else default


class TenderScoutAgent:
    def __init__(self):
        self.client = genai.Client(api_key=settings.GEMINI_API_KEY) if settings.GEMINI_API_KEY else None
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._skip_until = {}  # model -> time.monotonic() value when it may be tried again

    def _wait_for_slot(self):
        """Space calls at least MIN_INTERVAL_SECONDS apart."""
        with self._lock:
            wait = MIN_INTERVAL_SECONDS - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def _generate(self, contents, config):
        last_exc = None
        for model in MODEL_CHAIN:
            if self._skip_until.get(model, 0.0) > time.monotonic():
                continue
            for attempt in range(1, MAX_ATTEMPTS_PER_MODEL + 1):
                self._wait_for_slot()
                try:
                    return self.client.models.generate_content(
                        model=model, contents=contents, config=config
                    )
                except errors.APIError as exc:
                    last_exc = exc
                    if exc.code == 404:
                        logger.warning("Model %s is not available to this key; skipping it.", model)
                        self._skip_until[model] = time.monotonic() + COOLDOWN_SECONDS
                        break
                    if exc.code == 429 and _is_daily_quota(exc):
                        logger.warning(
                            "Daily quota exhausted for %s; using the next model for the next hour.", model
                        )
                        self._skip_until[model] = time.monotonic() + COOLDOWN_SECONDS
                        break
                    if exc.code == 429:
                        delay = _suggested_delay(exc, default=15.0 * attempt)
                    elif exc.code in TRANSIENT_CODES:
                        delay = min(2 ** attempt, 30) + random.random()
                    else:
                        # Retrying cannot help. A rejected key dooms the whole run;
                        # any other 4xx may be specific to this one tender.
                        if exc.code in (401, 403) or "api key" in str(exc).lower():
                            raise FatalAgentError(
                                f"Gemini rejected the API key ({exc.code}): {exc}"
                            ) from exc
                        raise TenderAgentError(
                            f"Gemini rejected this request ({exc.code}): {exc}"
                        ) from exc
                    if attempt == MAX_ATTEMPTS_PER_MODEL:
                        logger.warning("%s still failing (%s); moving to the next model.", model, exc.code)
                        break
                    logger.warning(
                        "%s returned %s (attempt %d/%d); retrying in %.0fs",
                        model, exc.code, attempt, MAX_ATTEMPTS_PER_MODEL, delay,
                    )
                    time.sleep(delay)
        raise QuotaExhaustedError(
            "All Gemini models are out of quota or unavailable right now "
            f"(last error: {str(last_exc)[:300] if last_exc else 'every model is on cooldown'})"
        )

    def analyze_tender(
        self, tender_title: str, tender_buyer: str, tender_description: str, company_profile: dict
    ) -> TenderAIAnalysisResult:
        if not self.client:
            raise TenderAgentError(
                "GEMINI_API_KEY is not set — cannot score tenders. Set it in .env, "
                "or explicitly call get_mock_analysis() for local development "
                "and label the result as mock in the API response."
            )

        system_instruction = f"""
You are an expert procurement analyst for {company_profile.get('company_name', 'HUERI Limited')}.
Company Profile Context:
- Description: {company_profile.get('description')}
- Core Capabilities: {', '.join(company_profile.get('core_services', []))}
- Target Regions: {', '.join(company_profile.get('target_geographies', []))}

Your task is to analyze tender opportunities, score their strategic fit, extract matched services, highlight gaps or disqualifiers, and output structured JSON.
"""

        user_prompt = f"""
Evaluate the following tender opportunity:
Title: {tender_title}
Buyer/Agency: {tender_buyer}
Description/Full Text: {tender_description}

Return a structured evaluation detailing match quality and action steps.
"""

        try:
            response = self._generate(
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json",
                    response_schema=TenderAIAnalysisResult,
                    temperature=0.2,
                ),
            )
            parsed_data = json.loads(response.text)
            return TenderAIAnalysisResult(**parsed_data)
        except TenderAgentError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("Gemini scoring failed for '%s': %s", tender_title, exc)
            raise TenderAgentError(f"Gemini scoring failed: {exc}") from exc

    @staticmethod
    def get_mock_analysis(tender_buyer: str) -> TenderAIAnalysisResult:
        """
        Explicit, opt-in mock analysis for local development ONLY. Never
        called automatically — main.py decides whether to use this, and
        must label the resulting tender as mock-scored in the response.
        """
        return TenderAIAnalysisResult(
            relevance_score=8.5,
            confidence_score=0.5,
            disqualification_risk="MEDIUM",
            is_fit=True,
            executive_summary=f"[MOCK] EIA and Environmental Audit project for {tender_buyer}.",
            matched_services=["Environmental Impact Assessment (EIA)", "Environmental Audit"],
            eligibility_gaps=["Requires active NEMA Firm License Registration 2026"],
            suggested_next_steps=["Download RFP documents", "Confirm lead expert availability"],
        )


tender_agent = TenderScoutAgent()
