from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backend.commerce.agent_routes import router as agent_router
from backend.commerce.approval_routes import router as approval_router
from backend.commerce.demo_routes import STATIC_DIR
from backend.commerce.demo_routes import page_router as demo_page_router
from backend.commerce.demo_routes import router as demo_router
from backend.commerce.product_routes import router as product_router

app = FastAPI(
    title="MandatePay",
    description=(
        "Safe autonomous purchasing with deterministic transaction controls. "
        "The demo UI is served at / and the API reference at /docs."
    ),
)

app.include_router(product_router)
app.include_router(approval_router)
app.include_router(agent_router)
app.include_router(demo_router)
app.include_router(demo_page_router)

# The demo UI is served by this same process, so `uvicorn backend.main:app`
# is the whole run instruction. Nothing under here is generated at runtime
# and nothing in it is read by any financial code path.
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
