"""Groloup fréquente Didi — serveur du jeu + tableau d'honneur (FastAPI + SQLite)."""
import json
import os
import re
import sqlite3
import time
from contextlib import closing

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

DB_PATH = os.environ.get("DB_PATH", "/data/scores.db")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

app = FastAPI(title="Groloup fréquente Didi", docs_url=None, redoc_url=None, openapi_url=None)


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


with closing(db()) as c:
    c.execute(
        """CREATE TABLE IF NOT EXISTS scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nom TEXT NOT NULL,
            points INTEGER NOT NULL,
            visites INTEGER NOT NULL,
            fleurs INTEGER NOT NULL,
            plantes INTEGER NOT NULL,
            temps INTEGER NOT NULL,
            badges INTEGER NOT NULL,
            ts INTEGER NOT NULL,
            ip TEXT
        )"""
    )
    c.commit()


class Score(BaseModel):
    nom: str = Field(min_length=1, max_length=20)
    points: int = Field(ge=0, le=200000)
    visites: int = Field(ge=1, le=99)
    fleurs: int = Field(ge=0, le=5000)
    plantes: int = Field(ge=0, le=5000)
    temps: int = Field(ge=0, le=200000)
    badges: int = Field(ge=0, le=50)


DERNIER_ENVOI: dict[str, float] = {}
ORDRE = "ORDER BY points DESC, visites ASC, temps ASC"


def ip_client(request: Request) -> str:
    # derrière un tunnel Cloudflare, la vraie IP du joueur est dans CF-Connecting-IP
    ip = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for") or ""
    ip = ip.split(",")[0].strip()
    if not ip and request.client:
        ip = request.client.host
    return ip


@app.get("/api/scores")
def lister():
    with closing(db()) as c:
        rows = c.execute(
            f"SELECT nom, points, visites, fleurs, plantes, temps, badges, ts FROM scores {ORDRE} LIMIT 20"
        ).fetchall()
    return Response(
        content=json.dumps([dict(r) for r in rows], ensure_ascii=False),
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/scores")
def ajouter(s: Score, request: Request):
    ip = ip_client(request)
    now = time.time()
    if now - DERNIER_ENVOI.get(ip, 0) < 10:
        raise HTTPException(status_code=429, detail="Un instant!")
    DERNIER_ENVOI[ip] = now
    nom = re.sub(r"[<>\x00-\x1f]", "", s.nom).strip()[:20] or "Anonyme"
    with closing(db()) as c:
        c.execute(
            "INSERT INTO scores (nom, points, visites, fleurs, plantes, temps, badges, ts, ip) VALUES (?,?,?,?,?,?,?,?,?)",
            (nom, s.points, s.visites, s.fleurs, s.plantes, s.temps, s.badges, int(now), ip),
        )
        c.commit()
        rang = c.execute("SELECT COUNT(*) FROM scores WHERE points > ?", (s.points,)).fetchone()[0] + 1
    return {"ok": True, "rang": rang}


@app.api_route("/favicon.ico", methods=["GET", "HEAD"])
def favicon():
    return Response(status_code=204)


@app.api_route("/", methods=["GET", "HEAD"])
def index():
    # no-cache: le navigateur revalide à chaque visite, donc une mise à jour du jeu est vue tout de suite
    return FileResponse(
        os.path.join(STATIC, "index.html"),
        media_type="text/html",
        headers={"Cache-Control": "no-cache"},
    )
