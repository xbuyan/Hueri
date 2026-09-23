import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from app.config import settings
from app.models import Base, Tender, TenderAnalysis, TenderStatus

engine = create_async_engine(settings.DATABASE_URL, echo=True)
async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

MOCK_TENDERS = [
    {
        "tender": {
            "title": "Environmental & Social Impact Assessment (ESIA) for Nairobi Metro",
            "buyer": "Ministry of Transport Kenya",
            "source": "PPIP",
            "url": "https://tenders.go.ke/tenders/ppip-esia-001",
            "external_id": "ppip-esia-001",
            "raw_summary": "Consultancy services required for comprehensive ESIA for modernizing public transit corridors in Nairobi.",
            "is_mock": True,
        },
        "analysis": {
            "relevance_score": 92.5,
            "is_fit": True,
            "executive_summary": "High alignment with HUERI's core ESIA expertise in urban infrastructure.",
            "matched_services": ["ESIA", "Environmental Impact Assessment (EIA)"],
            "eligibility_gaps": ["Requires 10+ years regional experience"],
            "suggested_next_steps": ["Submit EOI", "Form local JV partner"],
            "status": TenderStatus.NEW,
        }
    },
    {
        "tender": {
            "title": "Resettlement Action Plan (RAP) - Lake Victoria Basin",
            "buyer": "World Bank / NEMA",
            "source": "WorldBank",
            "url": "https://projects.worldbank.org/en/projects-operations/project-detail/wb-rap-002",
            "external_id": "wb-rap-002",
            "raw_summary": "Preparation of RAP and stakeholder engagement framework for coastal wetland protection project.",
            "is_mock": True,
        },
        "analysis": {
            "relevance_score": 88.0,
            "is_fit": True,
            "executive_summary": "Direct fit for HUERI social impact and resettlement consultancy.",
            "matched_services": ["Resettlement Action Plans (RAP)", "M&E"],
            "eligibility_gaps": [],
            "suggested_next_steps": ["Prepare technical proposal"],
            "status": TenderStatus.NEW,
        }
    }
]

async def seed_data():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as session:
        for item in MOCK_TENDERS:
            tender = Tender(**item["tender"])
            session.add(tender)
            await session.flush()

            analysis = TenderAnalysis(
                tender_id=tender.id,
                **item["analysis"]
            )
            session.add(analysis)

        await session.commit()
        print("\n--> Database successfully seeded!\n")

if __name__ == "__main__":
    asyncio.run(seed_data())
