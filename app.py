"""Groloup fréquente Didi — serveur du jeu, tableau d'honneur et statistiques (FastAPI + SQLite)."""
import base64
import html
import json
import os
import re
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

DB_PATH = os.environ.get("DB_PATH", "/data/scores.db")
STATS_PASS = os.environ.get("STATS_PASS", "")
TZ = ZoneInfo(os.environ.get("TZ_STATS", "America/Toronto"))
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

app = FastAPI(title="Groloup fréquente Didi", docs_url=None, redoc_url=None, openapi_url=None)


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


with closing(db()) as c:
    c.executescript(
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
        );
        CREATE TABLE IF NOT EXISTS evenements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            vid TEXT NOT NULL,
            sid TEXT NOT NULL,
            type TEXT NOT NULL,
            visite INTEGER,
            appareil TEXT,
            details TEXT
        );
        CREATE INDEX IF NOT EXISTS evt_ts ON evenements(ts);
        CREATE INDEX IF NOT EXISTS evt_vid ON evenements(vid);
        CREATE TABLE IF NOT EXISTS exclus (vid TEXT PRIMARY KEY, ts INTEGER NOT NULL);"""
    )
    c.commit()


def ip_client(request: Request) -> str:
    # derrière un tunnel Cloudflare, la vraie IP du joueur est dans CF-Connecting-IP
    ip = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for") or ""
    ip = ip.split(",")[0].strip()
    if not ip and request.client:
        ip = request.client.host
    return ip


# ---------------------------------------------------------------- tableau d'honneur

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


# ---------------------------------------------------------------- suivi des parties

TYPES = {"ouverture", "nouvelle", "reprise", "visite", "demande", "finale", "score"}
ID_RE = re.compile(r"^[A-Za-z0-9-]{6,40}$")
FENETRE: dict[str, deque] = defaultdict(deque)


class Evenement(BaseModel):
    vid: str = Field(min_length=6, max_length=40)
    sid: str = Field(min_length=6, max_length=40)
    type: str = Field(min_length=1, max_length=20)
    visite: int | None = Field(default=None, ge=0, le=99)
    d: dict | None = None


def appareil_de(ua: str) -> str:
    return "mobile" if re.search(r"Mobi|Android|iPhone|iPad|iPod", ua or "") else "ordi"


@app.post("/api/evt")
def evenement(e: Evenement, request: Request):
    if e.type not in TYPES or not ID_RE.match(e.vid) or not ID_RE.match(e.sid):
        return Response(status_code=204)
    # au plus 120 événements par 10 minutes par adresse
    ip, now = ip_client(request), time.time()
    q = FENETRE[ip]
    while q and now - q[0] > 600:
        q.popleft()
    if len(q) >= 120:
        return Response(status_code=204)
    q.append(now)
    details = None
    if e.d:
        propre = {str(k)[:20]: v for k, v in list(e.d.items())[:12] if isinstance(v, (int, float, str, bool))}
        details = json.dumps(propre, ensure_ascii=False)[:400]
    with closing(db()) as c:
        c.execute(
            "INSERT INTO evenements (ts, vid, sid, type, visite, appareil, details) VALUES (?,?,?,?,?,?,?)",
            (int(now), e.vid, e.sid, e.type, e.visite, appareil_de(request.headers.get("user-agent", "")), details),
        )
        c.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------- page de statistiques

def verifier(request: Request):
    if not STATS_PASS:
        raise HTTPException(status_code=404)
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("basic "):
        try:
            _, _, mdp = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
            if secrets.compare_digest(mdp.encode(), STATS_PASS.encode()):
                return
        except Exception:
            pass
    raise HTTPException(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Groloup stats", charset="UTF-8"'})


class Exclusion(BaseModel):
    vid: str = Field(min_length=6, max_length=40)
    exclure: bool


@app.post("/stats/exclure")
def exclure(x: Exclusion, request: Request):
    verifier(request)
    if not ID_RE.match(x.vid):
        raise HTTPException(status_code=400)
    with closing(db()) as c:
        if x.exclure:
            c.execute("INSERT OR IGNORE INTO exclus (vid, ts) VALUES (?, ?)", (x.vid, int(time.time())))
        else:
            c.execute("DELETE FROM exclus WHERE vid = ?", (x.vid,))
        c.commit()
    return {"ok": True}


def heure(ts: int, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return datetime.fromtimestamp(ts, TZ).strftime(fmt)


def tableau(entetes, lignes, num=()):
    th = "".join(f"<th{' class=n' if i in num else ''}>{html.escape(h)}</th>" for i, h in enumerate(entetes))
    corps = "".join(
        "<tr>" + "".join(f"<td{' class=n' if i in num else ''}>{html.escape(str(v))}</td>" for i, v in enumerate(l)) + "</tr>"
        for l in lignes
    ) or f"<tr><td colspan={len(entetes)}>Rien pour l'instant.</td></tr>"
    return f"<div class=t><table><thead><tr>{th}</tr></thead><tbody>{corps}</tbody></table></div>"


NOMS = {
    "ouverture": "Ouvre le jeu", "nouvelle": "Nouvelle partie", "reprise": "Reprend sa partie",
    "visite": "Termine une visite", "demande": "Demande en mariage", "finale": "Finale", "score": "Score enregistré",
}


@app.get("/stats", response_class=HTMLResponse)
def stats(request: Request):
    verifier(request)
    F = "vid NOT IN (SELECT vid FROM exclus)"
    with closing(db()) as c:
        q = lambda sql, *a: c.execute(sql, a).fetchall()
        un = lambda sql, *a: c.execute(sql, a).fetchone()[0]
        appareils = un(f"SELECT COUNT(DISTINCT vid) FROM evenements WHERE {F}")
        par_type = {r["type"]: r["n"] for r in q(f"SELECT type, COUNT(*) n FROM evenements WHERE {F} GROUP BY type")}
        mobiles = un(f"SELECT COUNT(DISTINCT vid) FROM evenements WHERE {F} AND appareil='mobile'")
        nb_scores = un("SELECT COUNT(*) FROM scores")
        exclus = un("SELECT COUNT(*) FROM exclus")
        liste_exclus = [r["vid"] for r in q("SELECT vid FROM exclus")]

        def atteint(cond, *a):
            return un(f"SELECT COUNT(DISTINCT vid) FROM evenements WHERE {F} AND {cond}", *a)

        entonnoir = [
            ("Ont ouvert le jeu", atteint("type='ouverture'")),
            ("Ont commencé une partie", atteint("type IN ('nouvelle','reprise')")),
            ("Ont terminé la visite 1", atteint("type='visite' AND visite>=1")),
            ("Ont terminé la visite 3", atteint("type='visite' AND visite>=3")),
            ("Ont terminé la visite 5", atteint("type='visite' AND visite>=5")),
            ("Ont fait la grande demande", atteint("type='demande'")),
            ("Ont vu la finale", atteint("type='finale'")),
            ("Ont inscrit leur nom", atteint("type='score'")),
        ]
        debut = int(time.time()) - 30 * 86400
        jours: dict[str, dict] = {}
        for r in q(f"SELECT ts, vid, type FROM evenements WHERE {F} AND ts >= ? ORDER BY ts", debut):
            j = jours.setdefault(heure(r["ts"], "%Y-%m-%d"), {"v": set(), "ouverture": 0, "parties": 0, "visite": 0, "finale": 0})
            j["v"].add(r["vid"])
            if r["type"] == "ouverture":
                j["ouverture"] += 1
            elif r["type"] in ("nouvelle", "reprise"):
                j["parties"] += 1
            elif r["type"] in ("visite", "finale"):
                j[r["type"]] += 1
        par_jour = [(d, len(v["v"]), v["ouverture"], v["parties"], v["visite"], v["finale"]) for d, v in sorted(jours.items(), reverse=True)]
        recents = [
            (heure(r["ts"]), r["vid"][:6], r["appareil"] or "", NOMS.get(r["type"], r["type"]), r["visite"] if r["visite"] is not None else "", r["details"] or "")
            for r in q(f"SELECT * FROM evenements WHERE {F} ORDER BY id DESC LIMIT 40")
        ]
        top = [(r["nom"], r["points"], r["visites"], heure(r["ts"], "%Y-%m-%d")) for r in q(f"SELECT * FROM scores {ORDRE} LIMIT 20")]

    pct_mobile = f"{round(100 * mobiles / appareils)} %" if appareils else "–"
    cartes = [
        ("Appareils", appareils), ("Ouvertures", par_type.get("ouverture", 0)),
        ("Parties commencées", par_type.get("nouvelle", 0) + par_type.get("reprise", 0)),
        ("Visites terminées", par_type.get("visite", 0)), ("Finales", par_type.get("finale", 0)),
        ("Scores", nb_scores), ("Sur mobile", pct_mobile),
    ]
    cartes_html = "".join(f"<div class=c><b>{html.escape(str(v))}</b><span>{html.escape(k)}</span></div>" for k, v in cartes)
    page = f"""<!doctype html><html lang=fr><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><meta name=robots content=noindex>
<title>Groloup — statistiques</title>
<style>
body{{margin:0;font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f6f4fb;color:#1b1035}}
main{{max-width:960px;margin:0 auto;padding:20px 16px 48px}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:17px;margin:28px 0 8px}}
.s{{color:#4a4560;margin:0 0 16px}}
.g{{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:10px}}
.c{{background:#fff;border:2px solid #1b1035;padding:10px 12px}} .c b{{display:block;font-size:24px}} .c span{{color:#4a4560;font-size:13px}}
.t{{overflow-x:auto;background:#fff;border:2px solid #1b1035}}
table{{border-collapse:collapse;width:100%;font-size:14px}} th,td{{padding:6px 10px;border-bottom:1px solid #ddd8ea;text-align:left;white-space:nowrap}}
th{{background:#efeaf8}} .n{{text-align:right;font-variant-numeric:tabular-nums}}
.moi{{margin-top:28px;padding:12px;background:#fff;border:2px dashed #1b1035}}
button{{font:inherit;padding:8px 14px;border:2px solid #1b1035;background:#ffe9a8;cursor:pointer}}
</style></head><body><main>
<h1>Groloup fréquente Didi — statistiques</h1>
<p class=s>Heures de {html.escape(str(TZ))}. Un « appareil » = un navigateur. {exclus} appareil(s) exclu(s) des chiffres.</p>
<div class=g>{cartes_html}</div>
<h2>Jusqu'où les joueurs se rendent</h2>
{tableau(["Étape", "Appareils"], entonnoir, num=(1,))}
<h2>Par jour (30 derniers jours)</h2>
{tableau(["Jour", "Appareils", "Ouvertures", "Parties", "Visites terminées", "Finales"], par_jour, num=(1, 2, 3, 4, 5))}
<h2>Tableau d'honneur</h2>
{tableau(["Nom", "Points", "Visites", "Date"], top, num=(1, 2))}
<h2>Derniers événements</h2>
{tableau(["Quand", "Appareil", "Type", "Action", "Visite", "Détails"], recents, num=(4,))}
<div class=moi><p id=moiTxt style="margin:0 0 8px">Cet appareil : …</p>
<button type=button id=moiBtn hidden></button></div>
</main>
<script>
(function () {{
  var exclus = {json.dumps(liste_exclus)};
  var vid = null; try {{ vid = JSON.parse(localStorage.getItem('gd_vid')); }} catch (e) {{}}
  var t = document.getElementById('moiTxt'), b = document.getElementById('moiBtn');
  if (!vid) {{ t.textContent = "Cet appareil n'a pas encore ouvert le jeu."; return; }}
  var dedans = exclus.indexOf(vid) < 0;
  t.textContent = 'Cet appareil : ' + String(vid).slice(0, 6) + (dedans ? ' — compté dans les statistiques.' : ' — exclu des statistiques.');
  b.textContent = dedans ? 'Exclure cet appareil (mes propres parties)' : 'Compter de nouveau cet appareil';
  b.hidden = false;
  b.onclick = function () {{
    fetch('stats/exclure', {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: JSON.stringify({{ vid: vid, exclure: dedans }}) }})
      .then(function (r) {{ if (r.ok) location.reload(); }});
  }};
}})();
</script></body></html>"""
    return HTMLResponse(page, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


# ---------------------------------------------------------------- le jeu

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
