#!/usr/bin/env python3
"""Gera data/demo.db + data/demo.sql para o live demo (Cloudflare D1).

Usa SOMENTE traduções de domínio público/licença livre (marcadas † no
dataset damarals/biblias): ALM1911, TB, BLIVRE. Sem FTS5 (D1) — a busca do
demo usa a coluna normalizada sot.text_norm. Sem vetores (sem semântica).
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_db as B  # noqa: E402

FREE = ("ALM1911", "TB", "BLIVRE")
HERE = Path(__file__).resolve().parent.parent
DEMO_DB = HERE / "data" / "demo.db"
DEMO_SQL = HERE / "data" / "demo.sql"


def deaccent(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def main() -> None:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else
               os.environ.get("BIBLIAS_CANONICAL", "../biblias/data/canonical"))
    tmp = Path(tempfile.mkdtemp(prefix="mob_demo_"))
    for t in FREE:
        os.symlink(src / t, tmp / t)
    B.CANON = tmp
    B.DB = DEMO_DB
    try:
        B.main()
    finally:
        shutil.rmtree(tmp)

    con = sqlite3.connect(DEMO_DB)
    con.execute("ALTER TABLE sot ADD COLUMN text_norm TEXT")
    con.executemany(
        "UPDATE sot SET text_norm=? WHERE book=? AND ch=? AND v=?",
        ((deaccent(t), b, c, v) for b, c, v, t in
         con.execute("SELECT book, ch, v, text FROM sot").fetchall()))
    con.commit()

    # dump sem as tabelas FTS (D1 não garante FTS5) e sem WAL pragmas
    tables = ["translations", "books", "verses", "sot", "crossrefs", "chapter_edges"]
    with DEMO_SQL.open("w", encoding="utf-8") as out:
        for t in tables:
            sql = con.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)
            ).fetchone()[0]
            out.write(f"DROP TABLE IF EXISTS {t};\n{sql};\n")
        for idx in con.execute("SELECT sql FROM sqlite_master WHERE type='index' "
                               "AND sql IS NOT NULL"):
            out.write(idx[0] + ";\n")
        for t in tables:
            rows = con.execute(f"SELECT * FROM {t}").fetchall()
            for i in range(0, len(rows), 50):
                chunk = rows[i:i + 50]
                vals = ",".join(
                    "(" + ",".join(
                        "NULL" if x is None else
                        str(x) if isinstance(x, (int, float)) else
                        "'" + str(x).replace("'", "''") + "'"
                        for x in r) + ")"
                    for r in chunk)
                out.write(f"INSERT INTO {t} VALUES {vals};\n")
    n = con.execute("SELECT count(*) FROM sot").fetchone()[0]
    x = con.execute("SELECT count(*) FROM crossrefs").fetchone()[0]
    con.close()
    print(f"demo: {n:,} versos SOT ({'+'.join(FREE)}), {x:,} crossrefs")
    print(f"→ {DEMO_DB} ({DEMO_DB.stat().st_size/2**20:.1f}MB) e "
          f"{DEMO_SQL} ({DEMO_SQL.stat().st_size/2**20:.1f}MB)")


if __name__ == "__main__":
    main()
