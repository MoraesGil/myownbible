#!/usr/bin/env python3
"""Gera demo/dist/ — o live demo 100% estático (Cloudflare Workers Assets).

Lê data/demo.db (gerado por build_demo_db.py) e emite:
  dist/index.html            (cópia de web/index.html)
  dist/data/meta.json        (books, bg_edges, center, totals, demo:true)
  dist/data/ch/BOOK-CH.json  (versos + versões + conexões por verso)
  dist/data/search.json      (índice compacto p/ busca no Worker)
Sem banco → sem limites de escrita → free tier garantido.
"""
import json
import shutil
import sqlite3
import unicodedata
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
DB = HERE / "data" / "demo.db"
DIST = HERE / "demo" / "dist"


def main() -> None:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    shutil.rmtree(DIST, ignore_errors=True)
    (DIST / "data" / "ch").mkdir(parents=True)
    shutil.copy(HERE / "web" / "index.html", DIST / "index.html")

    books = [dict(r) for r in con.execute("SELECT * FROM books ORDER BY ord")]
    names = {b["code"]: b["name"] for b in books}
    counts = defaultdict(dict)
    for r in con.execute("SELECT book, ch, count(*) n, avg(agreement) a FROM sot GROUP BY book, ch"):
        counts[r["book"]][r["ch"]] = [r["n"], round(r["a"], 3)]
    for b in books:
        b["verse_counts"] = [counts[b["code"]].get(c + 1, [0, 0]) for c in range(b["chapters"])]
    edges = [list(r) for r in con.execute(
        "SELECT src_book, src_ch, dst_book, dst_ch, weight FROM chapter_edges "
        "WHERE weight >= 12 ORDER BY weight DESC LIMIT 6000")]
    q1 = lambda s: con.execute(s).fetchone()
    cch = q1("SELECT book, ch, count(*) n FROM (SELECT src_book book, src_ch ch FROM crossrefs "
             "UNION ALL SELECT dst_book, dst_ch FROM crossrefs) GROUP BY book, ch ORDER BY n DESC LIMIT 1")
    cv = q1("SELECT book, ch, v, count(*) n FROM (SELECT src_book book, src_ch ch, src_v v FROM crossrefs "
            "UNION ALL SELECT dst_book, dst_ch, dst_v FROM crossrefs) GROUP BY book, ch, v ORDER BY n DESC LIMIT 1")
    ccv = con.execute(
        "SELECT v, count(*) n FROM ("
        " SELECT src_v v FROM crossrefs WHERE src_book=? AND src_ch=?"
        " UNION ALL SELECT dst_v FROM crossrefs WHERE dst_book=? AND dst_ch=?)"
        " GROUP BY v ORDER BY n DESC LIMIT 1",
        (cch["book"], cch["ch"], cch["book"], cch["ch"])).fetchone()
    meta = {
        "books": books, "bg_edges": edges, "demo": True,
        "center": {"chapter": [cch["book"], cch["ch"]], "n": cch["n"],
                   "chapter_verse": [cch["book"], cch["ch"], ccv["v"]],
                   "chapter_verse_n": ccv["n"],
                   "verse": [cv["book"], cv["ch"], cv["v"]], "verse_n": cv["n"]},
        "totals": {"verses": q1("SELECT count(*) c FROM sot")["c"],
                   "crossrefs": q1("SELECT count(*) c FROM crossrefs")["c"],
                   "translations": q1("SELECT count(*) c FROM translations")["c"]},
    }
    (DIST / "data" / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    # texto SOT indexável (o Worker faz a busca em memória)
    search = [[r["book"], r["ch"], r["v"], r["text_norm"], r["text"]]
              for r in con.execute("SELECT book, ch, v, text_norm, text FROM sot")]
    (DIST / "data" / "search.json").write_text(json.dumps(search, ensure_ascii=False), encoding="utf-8")

    # snippets de destino para as conexões
    sot_text = {(r["book"], r["ch"], r["v"]): r["text"]
                for r in con.execute("SELECT book, ch, v, text FROM sot")}

    # um arquivo por capítulo: versos + versões + conexões
    for b in books:
        code = b["code"]
        for ch in range(1, b["chapters"] + 1):
            verses = [dict(r) for r in con.execute(
                "SELECT v, text, origin, agreement, n_versions, missing FROM sot "
                "WHERE book=? AND ch=? ORDER BY v", (code, ch))]
            if not verses:
                continue
            versions = defaultdict(dict)
            for r in con.execute("SELECT trans, v, text FROM verses WHERE book=? AND ch=?", (code, ch)):
                versions[str(r["v"])][r["trans"]] = r["text"]
            links = defaultdict(list)
            for r in con.execute(
                    "SELECT src_v sv, dst_book b, dst_ch c, dst_v v, dst_v_end ve, votes, 'out' dir "
                    "FROM crossrefs WHERE src_book=? AND src_ch=? "
                    "UNION ALL "
                    "SELECT dst_v, src_book, src_ch, src_v, NULL, votes, 'in' "
                    "FROM crossrefs WHERE dst_book=? AND dst_ch=?", (code, ch, code, ch)):
                links[str(r["sv"])].append({
                    "b": r["b"], "c": r["c"], "v": r["v"], "ve": r["ve"],
                    "votes": r["votes"], "dir": r["dir"], "name": names.get(r["b"], r["b"]),
                    "snippet": (sot_text.get((r["b"], r["c"], r["v"]), "") or "")[:140]})
            for k in links:
                links[k] = sorted(links[k], key=lambda l: -l["votes"])[:60]
            nx = {str(v["v"]): len(links.get(str(v["v"]), [])) for v in verses}
            (DIST / "data" / "ch" / f"{code}-{ch}.json").write_text(json.dumps(
                {"book": code, "ch": ch, "verses": verses, "nx": nx,
                 "versions": versions, "links": links}, ensure_ascii=False), encoding="utf-8")
    con.close()
    nfiles = sum(1 for _ in DIST.rglob("*") if _.is_file())
    size = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file()) / 2**20
    print(f"dist: {nfiles} arquivos, {size:.1f}MB")


if __name__ == "__main__":
    main()
