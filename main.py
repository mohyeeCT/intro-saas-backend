from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routers import intro, jobs, settings

app = FastAPI(
    title="Page Intro Copy Production API",
    description="Generate SEO-optimised intro paragraphs with AI, GSC, and DataForSEO",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://intro.copypilot.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(intro.router, prefix="/api/intro", tags=["intro"])
app.include_router(jobs.router, prefix="/api/jobs", tags=["jobs"])
app.include_router(settings.router, prefix="/api/settings", tags=["settings"])


@app.get("/health")
def health():
    return {"status": "ok"}
