"""
Run the FastAPI dashboard (section 33).

Usage:
    PYTHONPATH=. python scripts/run_dashboard.py [--host 0.0.0.0] [--port 8000]

Open http://localhost:8000 for the UI, or hit /api/* endpoints
directly (see app/api/main.py). Read-only except the kill-switch
activate/deactivate endpoints.
"""
from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    uvicorn.run("app.api.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
