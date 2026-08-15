#!/usr/bin/env python3
"""myownbible — constrói data/bible.db a partir de biblias/data (read-only).

Camadas:
  1. verses     — todas as 18 traduções, alinhadas por (livro, capítulo, versículo)
  2. sot        — bíblia única por ELEIÇÃO: medoid lexical entre as versões,
                  com score de concordância e origem (nunca gera texto novo)
  3. sot_fts    — FTS5 para autocomplete
  4. crossrefs  — OpenBible.info (CC-BY), OSIS → USFM
  5. chapter_edges — agregado por capítulo para o arco do grafo
"""
import json
import os
import re
import sys
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

# pasta canonical/ do dataset https://github.com/damarals/biblias
CANON = Path(sys.argv[1] if len(sys.argv) > 1 else
             os.environ.get("BIBLIAS_CANONICAL", "../biblias/data/canonical"))
HERE = Path(__file__).resolve().parent.parent
DB = HERE / "data" / "bible.db"
XREF = HERE / "data" / "cross_references.txt"

OSIS2USFM = {
    "Gen": "GEN", "Exod": "EXO", "Lev": "LEV", "Num": "NUM", "Deut": "DEU",
    "Josh": "JOS", "Judg": "JDG", "Ruth": "RUT", "1Sam": "1SA", "2Sam": "2SA",
    "1Kgs": "1KI", "2Kgs": "2KI", "1Chr": "1CH", "2Chr": "2CH", "Ezra": "EZR",
    "Neh": "NEH", "Esth": "EST", "Job": "JOB", "Ps": "PSA", "Prov": "PRO",
    "Eccl": "ECC", "Song": "SNG", "Isa": "ISA", "Jer": "JER", "Lam": "LAM",
    "Ezek": "EZK", "Dan": "DAN", "Hos": "HOS", "Joel": "JOL", "Amos": "AMO",
    "Obad": "OBA", "Jonah": "JON", "Mic": "MIC", "Nah": "NAM", "Hab": "HAB",
    "Zeph": "ZEP", "Hag": "HAG", "Zech": "ZEC", "Mal": "MAL",
    "Matt": "MAT", "Mark": "MRK", "Luke": "LUK", "John": "JHN", "Acts": "ACT",
    "Rom": "ROM", "1Cor": "1CO", "2Cor": "2CO", "Gal": "GAL", "Eph": "EPH",
    "Phil": "PHP", "Col": "COL", "1Thess": "1TH", "2Thess": "2TH",
    "1Tim": "1TI", "2Tim": "2TI", "Titus": "TIT", "Phlm": "PHM", "Heb": "HEB",
    "Jas": "JAS", "1Pet": "1PE", "2Pet": "2PE", "1John": "1JN", "2John": "2JN",
    "3John": "3JN", "Jude": "JUD", "Rev": "REV",
}

_punct = re.compile(r"[^\w\s]", re.UNICODE)


def tokens(text: str) -> frozenset:
    t = unicodedata.normalize("NFKD", text.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return frozenset(_punct.sub(" ", t).split())


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def main() -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    DB.unlink(missing_ok=True)
    con = sqlite3.connect(DB)
    con.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE translations(code TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE books(code TEXT PRIMARY KEY, ord INTEGER, name TEXT,
                           testament TEXT, chapters INTEGER);
        CREATE TABLE verses(trans TEXT, book TEXT, ch INTEGER, v INTEGER, text TEXT,
                            PRIMARY KEY(trans, book, ch, v));
        CREATE TABLE sot(book TEXT, ch INTEGER, v INTEGER, text TEXT,
                         origin TEXT, agreement REAL, n_versions INTEGER,
                         missing TEXT, PRIMARY KEY(book, ch, v));
        CREATE VIRTUAL TABLE sot_fts USING fts5(ref, book_name, text,
                                                tokenize='unicode61 remove_diacritics 2');
        CREATE TABLE crossrefs(src_book TEXT, src_ch INTEGER, src_v INTEGER,
                               dst_book TEXT, dst_ch INTEGER, dst_v INTEGER,
                               dst_v_end INTEGER, votes INTEGER);
        CREATE INDEX xr_src ON crossrefs(src_book, src_ch, src_v);
        CREATE INDEX xr_dst ON crossrefs(dst_book, dst_ch, dst_v);
        CREATE TABLE chapter_edges(src_book TEXT, src_ch INTEGER,
                                   dst_book TEXT, dst_ch INTEGER, weight INTEGER,
                                   PRIMARY KEY(src_book, src_ch, dst_book, dst_ch));
    """)

    # ---- 1. carregar traduções ----------------------------------------
    trans_name = {}
    book_meta = {}           # code -> (ord, Counter(nomes), max_ch)
    corpus = defaultdict(dict)   # (book,ch,v) -> {trans: text}
    for tdir in sorted(p for p in CANON.iterdir() if p.is_dir()):
        tcode = tdir.name
        for f in tdir.glob("*.json"):
            d = json.loads(f.read_text(encoding="utf-8"))
            chapters = d.get("chapters") or []
            if not chapters:               # arquivo de metadata da tradução
                trans_name[tcode] = d.get("name") or tcode
                continue
            bcode = d.get("code") or f.stem
            ordn, names, mx = book_meta.get(bcode, (d.get("id") or 999, Counter(), 0))
            names[d.get("name") or bcode] += 1
            book_meta[bcode] = (d.get("id") or ordn, names, max(mx, len(chapters)))
            for c in chapters:
                for vv in c.get("verses", []):
                    tx = (vv.get("text") or "").strip()
                    if tx:
                        corpus[(bcode, c["number"], vv["number"])][tcode] = tx
        trans_name.setdefault(tcode, tcode)

    con.executemany("INSERT INTO translations VALUES (?,?)", sorted(trans_name.items()))
    for bcode, (ordn, names, mx) in book_meta.items():
        test = "VT" if ordn <= 39 else "NT"
        con.execute("INSERT INTO books VALUES (?,?,?,?,?)",
                    (bcode, ordn, names.most_common(1)[0][0], test, mx))
    bname = {b: n for b, (o, ns, m) in book_meta.items() for n in [ns.most_common(1)[0][0]]}

    con.executemany(
        "INSERT INTO verses VALUES (?,?,?,?,?)",
        ((t, b, c, v, tx) for (b, c, v), by in corpus.items() for t, tx in by.items()))
    all_trans = sorted(trans_name)

    # ---- 2. consenso por medoid ----------------------------------------
    sot_rows, fts_rows = [], []
    for (b, c, v), by in corpus.items():
        names = sorted(by)
        toks = [tokens(by[t]) for t in names]
        n = len(names)
        if n == 1:
            best, agree = 0, 0.0
        else:
            sims = [[0.0] * n for _ in range(n)]
            for i in range(n):
                for j in range(i + 1, n):
                    s = jaccard(toks[i], toks[j])
                    sims[i][j] = sims[j][i] = s
            means = [sum(row) / (n - 1) for row in sims]
            best = max(range(n), key=means.__getitem__)
            agree = sum(sum(row) for row in sims) / (n * (n - 1))
        missing = ",".join(t for t in all_trans if t not in by)
        sot_rows.append((b, c, v, by[names[best]], names[best],
                         round(agree, 4), n, missing))
        fts_rows.append((f"{b} {c}:{v}", bname[b], by[names[best]]))
    con.executemany("INSERT INTO sot VALUES (?,?,?,?,?,?,?,?)", sot_rows)
    con.executemany("INSERT INTO sot_fts VALUES (?,?,?)", fts_rows)

    # ---- 3. crossrefs ---------------------------------------------------
    ref_re = re.compile(r"^(\w+)\.(\d+)\.(\d+)$")
    xr_rows, agg = [], Counter()
    skipped = 0
    with XREF.open(encoding="utf-8") as fh:
        next(fh)  # header
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 3:
                continue
            src, dst, votes = parts[0], parts[1], int(parts[2])
            end_v = None
            if "-" in dst:
                dst, dend = dst.split("-", 1)
                m = ref_re.match(dend)
                end_v = int(m.group(3)) if m else None
            ms, md = ref_re.match(src), ref_re.match(dst)
            if not ms or not md:
                skipped += 1
                continue
            sb, db_ = OSIS2USFM.get(ms.group(1)), OSIS2USFM.get(md.group(1))
            if not sb or not db_:
                skipped += 1
                continue
            sc, sv = int(ms.group(2)), int(ms.group(3))
            dc, dv = int(md.group(2)), int(md.group(3))
            xr_rows.append((sb, sc, sv, db_, dc, dv, end_v, votes))
            agg[(sb, sc, db_, dc)] += 1
    con.executemany("INSERT INTO crossrefs VALUES (?,?,?,?,?,?,?,?)", xr_rows)
    con.executemany("INSERT INTO chapter_edges VALUES (?,?,?,?,?)",
                    ((k[0], k[1], k[2], k[3], w) for k, w in agg.items()))

    con.commit()
    q = lambda sql: con.execute(sql).fetchone()[0]
    print(f"verses: {q('SELECT count(*) FROM verses'):,}")
    print(f"sot:    {q('SELECT count(*) FROM sot'):,} "
          f"(concordância média {q('SELECT round(avg(agreement),4) FROM sot')})")
    print(f"xrefs:  {q('SELECT count(*) FROM crossrefs'):,} (skipped {skipped})")
    print(f"chapter_edges: {q('SELECT count(*) FROM chapter_edges'):,}")
    print("origem do SOT por tradução (top):")
    for t, nn in con.execute(
            "SELECT origin, count(*) FROM sot GROUP BY origin ORDER BY 2 DESC LIMIT 20"):
        print(f"  {t:9s} {nn:,}")
    con.close()


if __name__ == "__main__":
    main()
