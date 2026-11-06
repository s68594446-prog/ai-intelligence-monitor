from fastapi import FastAPI

from app.api.routes import router
from app.version import __version__


app = FastAPI(
    title="AI Intelligence Monitor",
    version=__version__,
    description="Auditable DRAM/HBM research harness with controlled fixed-source collection",
)
app.include_router(router)
