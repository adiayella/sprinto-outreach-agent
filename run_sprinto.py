"""Launcher for the Sprinto outreach agent:  .venv/bin/python run_sprinto.py   → http://127.0.0.1:8765"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("sprinto_outreach.api:app", host="127.0.0.1", port=8765, reload=False)
