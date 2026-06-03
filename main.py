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
        "https://copypilot.app",           # unified platform
        "https://intro.copypilot.app",     # legacy — keep during transition
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


from fastapi import Request
from fastapi.responses import JSONResponse


@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception):
    """Inject CORS headers on unhandled 500s.
    Railway EU edge strips CORS from 500 responses, causing misleading
    CORS errors in DevTools that mask the real server error."""
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc)},
        headers={
            "Access-Control-Allow-Origin": request.headers.get("origin", "*"),
            "Access-Control-Allow-Credentials": "true",
        },
    )
