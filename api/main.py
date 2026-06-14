from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.db import close_db, get_db, init_db
from api.routers import admin, auth, chat, checkout


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield
    await close_db()


app = FastAPI(title="Marble Vinyl Store", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(admin.router)
app.include_router(checkout.router)

# Serve frontend static files
frontend_dir = Path(__file__).parent.parent / "frontend"
if frontend_dir.exists():
    app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")

    @app.get("/")
    async def root():
        return FileResponse(str(frontend_dir / "customer.html"))

    @app.get("/customer.html")
    async def customer_page():
        return FileResponse(str(frontend_dir / "customer.html"))

    @app.get("/admin.html")
    async def admin_page():
        return FileResponse(str(frontend_dir / "admin.html"))


@app.get("/health")
async def health():
    return {"status": "ok"}


# Dependency override: return the shared DB connection
def _get_db_dep():
    return get_db()


app.dependency_overrides[get_db] = _get_db_dep
