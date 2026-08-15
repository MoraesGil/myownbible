/* myownbible — live demo 100% estático (Cloudflare Workers Assets, free tier).
   Dados pré-gerados por scripts/build_demo_static.py em demo/dist/data/;
   este Worker só roteia /api/* para os JSONs e faz a busca em memória.
   Só traduções de domínio público (ALM1911†, TB†, BLIVRE†); as notas do
   visitante ficam no localStorage (o front detecta meta.demo). */
"use strict";

const deaccent = s => s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();

const EXTRA_ALIASES = {
  gn: "GEN", ex: "EXO", lv: "LEV", nm: "NUM", dt: "DEU", js: "JOS", jz: "JDG",
  rt: "RUT", sl: "PSA", pv: "PRO", ec: "ECC", ct: "SNG", is: "ISA", jr: "JER",
  lm: "LAM", ez: "EZK", dn: "DAN", os: "HOS", jl: "JOL", am: "AMO", ob: "OBA",
  jn: "JON", mq: "MIC", na: "NAM", hc: "HAB", sf: "ZEP", ag: "HAG", zc: "ZEC",
  ml: "MAL", mt: "MAT", mc: "MRK", lc: "LUK", jo: "JHN", at: "ACT", rm: "ROM",
  gl: "GAL", ef: "EPH", fp: "PHP", cl: "COL", tt: "TIT", fm: "PHM", hb: "HEB",
  tg: "JAS", jd: "JUD", ap: "REV", jb: "JOB", job: "JOB",
};

let META = null, SEARCH = null, ALIASES = null, NAMES = null;

async function asset(env, req, path) {
  const r = await env.ASSETS.fetch(new URL(path, req.url));
  if (!r.ok) throw new Error(`asset ${path}: ${r.status}`);
  return r.json();
}

async function loadMeta(env, req) {
  if (META) return META;
  META = await asset(env, req, "/data/meta.json");
  NAMES = Object.fromEntries(META.books.map(b => [b.code, b.name]));
  ALIASES = { ...EXTRA_ALIASES };
  for (const b of META.books) {
    ALIASES[b.code.toLowerCase()] = b.code;
    ALIASES[deaccent(b.name)] = b.code;
    ALIASES[deaccent(b.name).replace(/ /g, "")] = b.code;
  }
  ALIASES.jo = "JHN";
  return META;
}

async function loadSearch(env, req) {
  if (!SEARCH) SEARCH = await asset(env, req, "/data/search.json");
  return SEARCH;
}

function parseRef(q) {
  const m = q.match(/^\s*([1-3]?\s?[a-zçãõáéíóúâêô]+)\.?\s*(\d+)?(?:[\s:.,]+(\d+))?\s*$/i);
  if (!m) return null;
  const raw = deaccent(m[1]).replace(/ /g, "");
  let code = ALIASES[raw];
  if (!code) {
    const cands = [...new Set(Object.entries(ALIASES)
      .filter(([k]) => k.startsWith(raw)).map(([, v]) => v))];
    if (cands.length !== 1) return null;
    code = cands[0];
  }
  return [code, m[2] ? +m[2] : null, m[3] ? +m[3] : null];
}

function snip(text, norm, nq) {
  const i = norm.indexOf(nq);
  if (i < 0) return text.slice(0, 110);
  const s = Math.max(0, i - 30);
  return (s ? "…" : "") + text.slice(s, i) + "<b>" + text.slice(i, i + nq.length) +
    "</b>" + text.slice(i + nq.length, s + 130);
}

async function chData(env, req, book, ch) {
  return asset(env, req, `/data/ch/${book}-${ch}.json`);
}

async function search(env, req, q) {
  await loadMeta(env, req);
  const out = [];
  const ref = parseRef(q);
  if (ref) {
    const [code, ch, v] = ref;
    try {
      const d = await chData(env, req, code, ch ?? 1);
      if (ch && v) {
        const r = d.verses.find(x => x.v === v);
        if (r) out.push({ ref: [code, ch, v], label: `${NAMES[code]} ${ch}:${v}`,
                          snippet: r.text.slice(0, 110), kind: "ref" });
      } else if (ch) {
        for (const r of d.verses.slice(0, 5))
          out.push({ ref: [code, ch, r.v], label: `${NAMES[code]} ${ch}:${r.v}`,
                     snippet: r.text.slice(0, 110), kind: "ref" });
      } else {
        out.push({ ref: [code, 1, 1], label: `${NAMES[code]} 1:1`,
                   snippet: "abrir livro", kind: "ref" });
      }
    } catch {}
  }
  const nq = deaccent(q).trim();
  if (nq && out.length < 10) {
    const S = await loadSearch(env, req);
    for (const [b, c, v, norm, text] of S) {
      if (!norm.includes(nq)) continue;
      out.push({ ref: [b, c, v], label: `${NAMES[b]} ${c}:${v}`,
                 snippet: snip(text, norm, nq), kind: "texto" });
      if (out.length >= 10) break;
    }
  }
  return out;
}

async function queryCenter(env, req, q) {
  await loadMeta(env, req);
  const nq = deaccent(q).trim();
  const matches = [];
  let total = 0;
  if (nq) {
    const S = await loadSearch(env, req);
    for (const [b, c, v, norm, text] of S) {
      if (!norm.includes(nq)) continue;
      total++;
      if (matches.length < 80)
        matches.push({ ref: [b, c, v], label: `${NAMES[b]} ${c}:${v}`,
                       snippet: snip(text, norm, nq), kind: "texto" });
    }
  }
  return { q, total, matches };
}

const json = (o, s = 200) => new Response(JSON.stringify(o), {
  status: s,
  headers: { "content-type": "application/json; charset=utf-8",
             "cache-control": "public, max-age=3600" },
});

export default {
  async fetch(req, env) {
    const u = new URL(req.url);
    if (!u.pathname.startsWith("/api/")) return env.ASSETS.fetch(req);
    const p = u.searchParams;
    try {
      switch (u.pathname) {
        case "/api/meta":
          return json(await loadMeta(env, req));
        case "/api/search":
          return json(await search(env, req, p.get("q") ?? ""));
        case "/api/query_center":
          return json(await queryCenter(env, req, p.get("q") ?? ""));
        case "/api/chapter": {
          const d = await chData(env, req, p.get("book"), +p.get("ch"));
          const verses = d.verses.map(v => ({ ...v, nx: d.nx[String(v.v)] ?? 0 }));
          return json({ book: d.book, ch: d.ch, verses });
        }
        case "/api/verse": {
          const v = +p.get("v");
          const d = await chData(env, req, p.get("book"), +p.get("ch"));
          const sot = d.verses.find(x => x.v === v) ?? null;
          const vers = d.versions[String(v)] ?? {};
          return json({
            sot,
            versions: Object.entries(vers).sort()
              .map(([trans, text]) => ({ trans, text })),
            links: d.links[String(v)] ?? [],
          });
        }
        case "/api/semantic":
          return json({ error: "sem busca semântica no demo — rode localmente" });
        default:
          return json({ error: "not found" }, 404);
      }
    } catch (e) {
      return json({ error: String(e) }, 500);
    }
  },
};
