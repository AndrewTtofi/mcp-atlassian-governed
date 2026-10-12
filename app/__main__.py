"""`python -m app`: serve MCP over Streamable HTTP."""

import uvicorn

from app.server import build_app
from app.settings import settings

if __name__ == "__main__":
    uvicorn.run(
        build_app(), host=settings.host, port=settings.port, log_level=settings.log_level.lower()
    )
