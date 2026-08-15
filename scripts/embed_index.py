#!/usr/bin/env python3
"""Indexa os versos SOT num endpoint OpenAI-compatível de embeddings
(Ollama, LM Studio, llama.cpp, OpenAI…) — configure MOB_EMBED_URL/MOB_EMBED_MODEL.

Retomável: pula versos já indexados. Aborta se a RAM livre da máquina < 4GB.
Uso:  python3 scripts/embed_index.py [batch=48]
"""
import json
import os
import sqlite3
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "bible.db"
URL = os.environ.get("MOB_EMBED_URL", "http://127.0.0.1:8090/v1/embeddings")
MODEL = os.environ.get("MOB_EMBED_MODEL", "mylocal/embed")
BATCH = int(sys.argv[1]) if len(sys.argv) > 1 else 48


def ram_free_gb() -> float:
    out = subprocess.check_output(["vm_stat"], text=True)
    pages = 0
    for line in out.splitlines():
        if any(k in line for k in ("free", "inactive", "speculative")):
            pages += int(line.split()[-1].rstrip("."))
    return pages * 16384 / 2**30


def embed(texts):
    req = urllib.request.Request(
        URL, data=json.dumps({"model": MODEL, "input": texts}).encode(),
        headers={"Content-Type": "application/json"})
    d = json.loads(urllib.request.urlopen(req, timeout=600).read())
    return [e["embedding"] for e in sorted(d["data"], key=lambda e: e["index"])]


def main():
    if ram_free_gb() < 4:
        sys.exit("RAM livre < 4GB — abortando (regra do piso).")
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS vectors("
                "book TEXT, ch INTEGER, v INTEGER, vec BLOB,"
                "PRIMARY KEY(book, ch, v))")
    todo = con.execute(
        "SELECT s.book, s.ch, s.v, s.text FROM sot s "
        "LEFT JOIN vectors x ON x.book=s.book AND x.ch=s.ch AND x.v=s.v "
        "WHERE x.book IS NULL ORDER BY s.book, s.ch, s.v").fetchall()
    total = len(todo)
    print(f"{total:,} versos a embedar (batch {BATCH})", flush=True)
    t0 = time.time()
    for i in range(0, total, BATCH):
        chunk = todo[i:i + BATCH]
        vecs = embed([r[3] for r in chunk])
        con.executemany(
            "INSERT OR REPLACE INTO vectors VALUES (?,?,?,?)",
            ((r[0], r[1], r[2], struct.pack(f"{len(v)}f", *v))
             for r, v in zip(chunk, vecs)))
        con.commit()
        done = i + len(chunk)
        if done % (BATCH * 20) < BATCH or done == total:
            rate = done / (time.time() - t0)
            eta = (total - done) / rate / 60 if rate else 0
            print(f"{done:,}/{total:,} ({rate:.0f}/s, eta {eta:.0f}min)", flush=True)
    print("índice semântico completo.", flush=True)
    con.close()


if __name__ == "__main__":
    main()
