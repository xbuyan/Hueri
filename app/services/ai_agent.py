import json
import logging
from google import genai
from google.genai import types
from app.config import settings
from app.schemas import TenderAIAnalysisResult

logger = logging.getLogger("services.ai_agent")

class TenderAgentError(Exception):
    pass

class TenderScoutAgent:
    def __init__(self):
        self.client = genai.Client(api_key=settings.GEMINI_API_KEY) if settings.GEMINI_API_KEY else None

    def analyze_tender(self, tender_title: str, tender_buyer: str, tender_description: str, company_profile: dict) -> TenderAIAnalysisResult:
        if not self.client:
            raise TenderAgentError("GEMINI_API_KEY is missing")

        system_instruction = f"""
You are the Lead Procurement & Technical Risk Evaluator for {company_profile.get('company_name', 'HUERI Limited')}.
Company Profile Context:
- Description: {company_profile.get('description')}
- Core Capabilities: {', '.join(company_profile.get('core_services', []))}
- Target Regions: {', '.join(company_profile.get('target_geographies', []))}

EVALUATION WEIGHTING & SCORING MATRIX (0.0 to 10.0):
1. Service Capability Match (40% Weight): EIA, ESIA, Environmental Audits, RAPs, M&E, Feasibility Studies.
2. Geographic Alignment (20% Weight): Priority for Kenya, Uganda, Tanzania, and East Africa.
3. Regulatory & Licencing Criteria (20% Weight): Explicitly identify mandatory NEMA Lead Expert / Firm licencing or local statutory registrations.
4. Scale & Strategic Fit (20% Weight): Size, consultancy vs civil works distinction.

DISQUALIFICATION & PENALTY RULES:
- If the tender is strictly for physical works/construction without consultancy/studies, apply a -5.0 penalty.
- If mandatory NEMA registration is required and not mentioned, tag in eligibility gaps and set disqualification_risk = HIGH.

Return structured JSON adhering to the output schema.
"""

        user_prompt = f"""
Evaluate Opportunity:
Title: {tender_title}
Buyer: {tender_buyer}
Description: {tender_description}
"""

        response = self.client.models.generate_content(
            model='gemini-2.5-flash',
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                response_schema=TenderAIAnalysisResult,
                temperature=0.1,
            ),
        )
        return TenderAIAnalysisResult(**json.loads(response.text))

tender_agent = TenderScoutAgent()
