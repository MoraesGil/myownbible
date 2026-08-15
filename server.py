#!/usr/bin/env python3
"""myownbible — servidor local (stdlib puro).

Uso:  python3 server.py [porta]     (default 8341)
"""
import json
import re
import os
import sqlite3
import struct
import sys
import unicodedata
import urllib.request
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
DB = HERE / "data" / "bible.db"
NOTES_DB = HERE / "data" / "notes.db"   # separado: sobrevive a rebuilds do bible.db
WEB = HERE / "web"
# qualquer endpoint OpenAI-compatível de embeddings (Ollama, LM Studio, OpenAI…)
EMBED_URL = os.environ.get("MOB_EMBED_URL", "http://127.0.0.1:8090/v1/embeddings")
EMBED_MODEL = os.environ.get("MOB_EMBED_MODEL", "mylocal/embed")


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def notes_db():
    con = sqlite3.connect(NOTES_DB)
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE IF NOT EXISTS notes("
                "book TEXT, ch INTEGER, v INTEGER DEFAULT 0, text TEXT,"
                "updated TEXT DEFAULT (datetime('now')),"
                "PRIMARY KEY(book, ch, v))")     # v=0 → nota do capítulo
    return con


def notes_get(book: str, ch: int):
    con = notes_db()
    out = {"chapter": None, "verses": {}}
    for r in con.execute("SELECT v, text FROM notes WHERE book=? AND ch=?", (book, ch)):
        if r["v"] == 0:
            out["chapter"] = r["text"]
        else:
            out["verses"][str(r["v"])] = r["text"]
    con.close()
    return out


def notes_set(book: str, ch: int, v: int, text: str):
    con = notes_db()
    if text.strip():
        con.execute("INSERT INTO notes(book, ch, v, text, updated) "
                    "VALUES (?,?,?,?,datetime('now')) "
                    "ON CONFLICT(book, ch, v) DO UPDATE SET text=excluded.text, "
                    "updated=excluded.updated", (book, ch, v, text))
    else:
        con.execute("DELETE FROM notes WHERE book=? AND ch=? AND v=?", (book, ch, v))
    con.commit()
    con.close()
    return {"ok": True}


def notes_all():
    con = notes_db()
    out = {f"{r['book']}.{r['ch']}.{r['v']}": r["text"]
           for r in con.execute("SELECT book, ch, v, text FROM notes")}
    con.close()
    return out


def note_map():
    con = notes_db()
    out = sorted({f"{r['book']}.{r['ch']}" for r in
                  con.execute("SELECT DISTINCT book, ch FROM notes")})
    con.close()
    return out


def deaccent(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


@lru_cache(maxsize=1)
def meta():
    con = db()
    books = [dict(r) for r in con.execute("SELECT * FROM books ORDER BY ord")]
    counts = {}
    for r in con.execute("SELECT book, ch, count(*) n, avg(agreement) a "
                         "FROM sot GROUP BY book, ch"):
        counts.setdefault(r["book"], {})[r["ch"]] = [r["n"], round(r["a"], 3)]
    for b in books:
        b["verse_counts"] = [counts[b["code"]].get(c + 1, [0, 0])
                             for c in range(b["chapters"])]
    edges = [list(r) for r in con.execute(
        "SELECT src_book, src_ch, dst_book, dst_ch, weight FROM chapter_edges "
        "WHERE weight >= 12 ORDER BY weight DESC LIMIT 6000")]
    total = con.execute("SELECT count(*) FROM sot").fetchone()[0]
    xr = con.execute("SELECT count(*) FROM crossrefs").fetchone()[0]
    # ponto central da bíblia: capítulo e verso com maior grau de conexões
    cch = con.execute(
        "SELECT book, ch, count(*) n FROM ("
        " SELECT src_book book, src_ch ch FROM crossrefs"
        " UNION ALL SELECT dst_book, dst_ch FROM crossrefs)"
        " GROUP BY book, ch ORDER BY n DESC LIMIT 1").fetchone()
    cv = con.execute(
        "SELECT book, ch, v, count(*) n FROM ("
        " SELECT src_book book, src_ch ch, src_v v FROM crossrefs"
        " UNION ALL SELECT dst_book, dst_ch, dst_v FROM crossrefs)"
        " GROUP BY book, ch, v ORDER BY n DESC LIMIT 1").fetchone()
    con.close()
    return {"books": books, "bg_edges": edges,
            "center": {"chapter": [cch["book"], cch["ch"]], "n": cch["n"],
                       "verse": [cv["book"], cv["ch"], cv["v"]], "verse_n": cv["n"]},
            "totals": {"verses": total, "crossrefs": xr, "translations": 18}}


@lru_cache(maxsize=1)
def book_aliases():
    alias = {}
    for b in meta()["books"]:
        code, name = b["code"], b["name"]
        for a in {code.lower(), deaccent(name), deaccent(name).replace(" ", "")}:
            alias[a] = code
    # abreviações usuais pt-br
    extra = {"gn": "GEN", "ex": "EXO", "lv": "LEV", "nm": "NUM", "dt": "DEU",
             "js": "JOS", "jz": "JDG", "rt": "RUT", "sl": "PSA", "pv": "PRO",
             "ec": "ECC", "ct": "SNG", "is": "ISA", "jr": "JER", "lm": "LAM",
             "ez": "EZK", "dn": "DAN", "os": "HOS", "jl": "JOL", "am": "AMO",
             "ob": "OBA", "jn": "JON", "mq": "MIC", "na": "NAM", "hc": "HAB",
             "sf": "ZEP", "ag": "HAG", "zc": "ZEC", "ml": "MAL", "mt": "MAT",
             "mc": "MRK", "lc": "LUK", "jo": "JHN", "at": "ACT", "rm": "ROM",
             "gl": "GAL", "ef": "EPH", "fp": "PHP", "cl": "COL", "tt": "TIT",
             "fm": "PHM", "hb": "HEB", "tg": "JAS", "jd": "JUD", "ap": "REV",
             "salmos": "PSA", "atos": "ACT", "apocalipse": "REV"}
    for k, v in extra.items():
        alias.setdefault(k, v)
    # desambiguação convencional pt-br: "jo" = João; Jó fica em "jó"/"jb"/"job"
    alias["jo"] = "JHN"
    alias["jb"] = "JOB"
    alias["job"] = "JOB"
    return alias


REF_RE = re.compile(r"^\s*([1-3]?\s?[a-zçãõáéíóúâêô]+)\.?\s*(\d+)?(?:[\s:.,]+(\d+))?\s*$",
                    re.IGNORECASE)


def parse_ref(q: str):
    """'jo 3 16' / 'gênesis 1:1' / 'sl 23' → (book, ch, v) parciais."""
    m = REF_RE.match(q)
    if not m:
        return None
    raw = deaccent(m.group(1)).replace(" ", "")
    aliases = book_aliases()
    code = aliases.get(raw)
    if not code:
        cands = sorted({v for k, v in aliases.items() if k.startswith(raw)})
        if len(cands) != 1:
            return None
        code = cands[0]
    return code, (int(m.group(2)) if m.group(2) else None), \
        (int(m.group(3)) if m.group(3) else None)


def search(q: str, limit=10):
    out = []
    ref = parse_ref(q)
    names = {b["code"]: b["name"] for b in meta()["books"]}
    if ref:
        code, ch, v = ref
        con = db()
        if ch and v:
            r = con.execute("SELECT * FROM sot WHERE book=? AND ch=? AND v=?",
                            (code, ch, v)).fetchone()
            if r:
                out.append({"ref": [code, ch, v], "label": f"{names[code]} {ch}:{v}",
                            "snippet": r["text"][:110], "kind": "ref"})
        elif ch:
            for r in con.execute("SELECT * FROM sot WHERE book=? AND ch=? "
                                 "ORDER BY v LIMIT 5", (code, ch)):
                out.append({"ref": [code, ch, r["v"]],
                            "label": f"{names[code]} {ch}:{r['v']}",
                            "snippet": r["text"][:110], "kind": "ref"})
        else:
            out.append({"ref": [code, 1, 1], "label": f"{names[code]} 1:1",
                        "snippet": "abrir livro", "kind": "ref"})
        con.close()
    if len(out) < limit:
        con = db()
        fq = " ".join(f'"{t}"*' for t in re.findall(r"\w+", deaccent(q)) if t)
        if fq:
            try:
                for r in con.execute(
                        "SELECT ref, snippet(sot_fts, 2, '<b>', '</b>', '…', 14) s "
                        "FROM sot_fts WHERE sot_fts MATCH ? ORDER BY rank LIMIT ?",
                        (fq, limit - len(out))):
                    b, cv = r["ref"].split(" ")
                    ch, v = cv.split(":")
                    out.append({"ref": [b, int(ch), int(v)],
                                "label": f"{names[b]} {ch}:{v}",
                                "snippet": r["s"], "kind": "texto"})
            except sqlite3.OperationalError:
                pass
        con.close()
    return out


def query_center(q: str, cap=80):
    """A busca como nó central: camada 'texto' = FRASE EXATA (FTS phrase);
    o resto do sentido vem pela camada semântica."""
    con = db()
    names = {b["code"]: b["name"] for b in meta()["books"]}
    toks = re.findall(r"\w+", deaccent(q))
    fq = '"' + " ".join(toks) + '"' if toks else ""
    total, matches, seen = 0, [], set()
    if fq:
        try:
            total = con.execute(
                "SELECT count(*) FROM sot_fts WHERE sot_fts MATCH ?", (fq,)).fetchone()[0]
            for r in con.execute(
                    "SELECT ref, snippet(sot_fts, 2, '<b>', '</b>', '…', 16) s "
                    "FROM sot_fts WHERE sot_fts MATCH ? ORDER BY rank LIMIT ?", (fq, cap)):
                b, cv = r["ref"].split(" ")
                ch, v = cv.split(":")
                key = (b, int(ch), int(v))
                seen.add(key)
                matches.append({"ref": list(key), "label": f"{names[b]} {ch}:{v}",
                                "snippet": r["s"], "kind": "texto"})
        except sqlite3.OperationalError:
            pass
    sem = semantic(q, limit=14)
    # piso adaptativo: sem match literal, a vizinhança semântica é a resposta
    floor = 0.62 if matches else 0.45
    if isinstance(sem, list):
        for m in sem:
            key = tuple(m["ref"])
            if key not in seen and m["score"] >= floor and len(matches) < cap + 14:
                matches.append({**m, "kind": "semântica"})
    con.close()
    return {"q": q, "total": total, "matches": matches}


def chapter(book: str, ch: int):
    con = db()
    rows = [dict(r) for r in con.execute(
        "SELECT v, text, origin, agreement, n_versions, missing FROM sot "
        "WHERE book=? AND ch=? ORDER BY v", (book, ch))]
    xr = {r["src_v"]: r["n"] for r in con.execute(
        "SELECT src_v, count(*) n FROM ("
        " SELECT src_v FROM crossrefs WHERE src_book=? AND src_ch=?"
        " UNION ALL SELECT dst_v FROM crossrefs WHERE dst_book=? AND dst_ch=?)"
        " GROUP BY src_v", (book, ch, book, ch))}
    con.close()
    for r in rows:
        r["nx"] = xr.get(r["v"], 0)
    return {"book": book, "ch": ch, "verses": rows}


def verse(book: str, ch: int, v: int):
    con = db()
    sot = con.execute("SELECT * FROM sot WHERE book=? AND ch=? AND v=?",
                      (book, ch, v)).fetchone()
    versions = [dict(r) for r in con.execute(
        "SELECT trans, text FROM verses WHERE book=? AND ch=? AND v=? ORDER BY trans",
        (book, ch, v))]
    links = [dict(r) for r in con.execute(
        "SELECT dst_book b, dst_ch c, dst_v v, dst_v_end ve, votes, 'out' dir "
        "FROM crossrefs WHERE src_book=? AND src_ch=? AND src_v=? "
        "UNION ALL "
        "SELECT src_book, src_ch, src_v, NULL, votes, 'in' "
        "FROM crossrefs WHERE dst_book=? AND dst_ch=? AND dst_v=? "
        "ORDER BY votes DESC LIMIT 60", (book, ch, v, book, ch, v))]
    names = {b["code"]: b["name"] for b in meta()["books"]}
    con2 = db()
    for l in links:
        l["name"] = names.get(l["b"], l["b"])
        t = con2.execute("SELECT text FROM sot WHERE book=? AND ch=? AND v=?",
                         (l["b"], l["c"], l["v"])).fetchone()
        l["snippet"] = (t["text"][:140] if t else "")
    con2.close()
    con.close()
    return {"sot": dict(sot) if sot else None, "versions": versions, "links": links}


_NUMS = {"1": "um", "2": "dois", "3": "três", "4": "quatro", "5": "cinco",
         "6": "seis", "7": "sete", "8": "oito", "9": "nove", "10": "dez",
         "11": "onze", "12": "doze", "13": "treze", "14": "catorze",
         "15": "quinze", "16": "dezesseis", "17": "dezessete", "18": "dezoito",
         "19": "dezenove", "20": "vinte", "30": "trinta", "40": "quarenta",
         "50": "cinquenta", "70": "setenta", "100": "cem", "1000": "mil"}


def sem_query_text(q: str) -> str:
    """Numerais por extenso: o texto bíblico não escreve '10 pragas'."""
    return re.sub(r"\b(\d+)\b", lambda m: _NUMS.get(m.group(1), m.group(1)), q)


def semantic(q: str, limit=10):
    con = db()
    has = con.execute("SELECT name FROM sqlite_master WHERE name='vectors'").fetchone()
    if not has:
        con.close()
        return {"error": "índice semântico ainda não construído (rode scripts/embed_index.py)"}
    try:
        import numpy as np
    except ImportError:
        con.close()
        return {"error": "numpy não instalado"}
    req = urllib.request.Request(
        EMBED_URL,
        data=json.dumps({"model": EMBED_MODEL, "input": [sem_query_text(q)]}).encode(),
        headers={"Content-Type": "application/json"})
    emb = json.loads(urllib.request.urlopen(req, timeout=300).read())
    qv = np.array(emb["data"][0]["embedding"], dtype=np.float32)
    rows = con.execute("SELECT book, ch, v, vec FROM vectors").fetchall()
    mat = np.frombuffer(b"".join(r["vec"] for r in rows), dtype=np.float32)
    mat = mat.reshape(len(rows), -1)
    sims = mat @ qv
    idx = sims.argsort()[::-1][:limit]
    names = {b["code"]: b["name"] for b in meta()["books"]}
    out = []
    for i in idx:
        r = rows[int(i)]
        t = con.execute("SELECT text FROM sot WHERE book=? AND ch=? AND v=?",
                        (r["book"], r["ch"], r["v"])).fetchone()
        out.append({"ref": [r["book"], r["ch"], r["v"]],
                    "label": f"{names[r['book']]} {r['ch']}:{r['v']}",
                    "snippet": t["text"][:140] if t else "",
                    "score": round(float(sims[int(i)]), 4), "kind": "semântica"})
    con.close()
    return out


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                body = (WEB / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/meta":
                self._json(meta())
            elif u.path == "/api/search":
                self._json(search(qs.get("q", "")))
            elif u.path == "/api/semantic":
                self._json(semantic(qs.get("q", "")))
            elif u.path == "/api/chapter":
                self._json(chapter(qs["book"], int(qs["ch"])))
            elif u.path == "/api/verse":
                self._json(verse(qs["book"], int(qs["ch"]), int(qs["v"])))
            elif u.path == "/api/query_center":
                self._json(query_center(qs.get("q", "")))
            elif u.path == "/api/notes":
                self._json(notes_get(qs["book"], int(qs["ch"])))
            elif u.path == "/api/note_map":
                self._json(note_map())
            elif u.path == "/api/notes_all":
                self._json(notes_all())
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001 — servidor local de estudo
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            if u.path == "/api/note":
                self._json(notes_set(body["book"], int(body["ch"]),
                                     int(body.get("v") or 0), body.get("text", "")))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, 500)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8341
    print(f"myownbible em http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
