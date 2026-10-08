"""Owner: backend member. Add provider configuration here."""
import os

FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:5500")
DATABASE_PATH = os.getenv("DATABASE_PATH", "greenprompt.db")
