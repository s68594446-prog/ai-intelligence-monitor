from fastapi import FastAPI

from app.api.routes import router


app = FastAPI(
    title="AI Intelligence Monitor",
    version="0.2.0",
    description="Auditable DRAM/HBM research harness with controlled fixed-source collection",
)
app.include_router(router)
