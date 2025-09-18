
import os
import re
import time
import requests
import unicodedata
import streamlit as st
from rapidfuzz import fuzz, process
from openai import OpenAI

# ========= CONFIG =========
DEFAULT_SAFE_TARGET = 10          # počet "bezpečných" návrhů ve výstupu
ARES_COUNT = 60                   # kolik záznamů tahat z ARES pro porovnání
UA = "cz-name-checker/1.3 (+contact: owner@example.com)"

# ========= API KEY =========
def _get_api_key():
    key = os.getenv("OPENAI_API_KEY")
    if key:
        return key
    try:
        return st.secrets["OPENAI_API_KEY"]
    except Exception:
        return None

_api_key = _get_api_key()
if not _api_key:
    st.error("Chybí OPENAI_API_KEY. Nastav v Railway/Variables nebo ve Streamlit Secrets.")
    st.stop()
client = OpenAI(api_key=_api_key)

# ========= NORMALIZATION =========
LEGAL_SUFFIXES = [
    "s.r.o.", "sro", "a.s.", "as", "k.s.", "ks", "v.o.s.", "vos", "spol. s r.o.", "spol s r o"
]
GENERIC_WORDS = [
    "cz","czech","praha","brno","plzen","ostrava","group","holding","solutions","consulting",
    "system","systems","technology","technologies","services","service","studio","company","co",
    "global","international","advisory","adviser","advisers","media","marketing","agency","digital"
]

def strip_diacritics(s: str) -> str:
    norm = unicodedata.normalize("NFD", s or "")
    return "".join(ch for ch in norm if unicodedata.category(ch) != "Mn")

def norm_core(name: str) -> str:
    if not name:
        return ""
    s = name.lower().strip()
    s = strip_diacritics(s)
    for suf in LEGAL_SUFFIXES:
        s = s.replace(suf, " ")
    s = re.sub(r"[^0-9a-zA-Z\s]", " ", s)
    s = " ".join(s.split())
    parts = [p for p in s.split() if p not in GENERIC_WORDS]
    return " ".join(parts)

# ========= ARES SEARCH (v2 REST, zdroj OR) s retry =========
def ares_query(obchodni_jmeno: str, count: int = ARES_COUNT):
    base = "https://ares.gov.cz/ekonomicke-subjekty-v2/ekonomicke-subjekty"
    params = {
        "obchodniJmeno": obchodni_jmeno,
        "pocet": str(count),
        "razeni": "obchodniJmeno@asc",
        "zdroj": "OR"
    }
    headers = {"Accept": "application/json", "User-Agent": UA}

    last_err = None
    for attempt in range(3):
        try:
            r = requests.get(base, params=params, headers=headers, timeout=20)
            r.raise_for_status()
            data = r.json()
            items = data.get("ekonomickeSubjekty", []) or data.get("vysledky", [])
            out = []
            for it in items:
                of = it.get("obchodniJmeno") or it.get("obchodniJmenoText")
                ico = it.get("ico")
                if of:
                    out.append({"name": of, "ico": ico})
            return out
        except Exception as e:
            last_err = e
            time.sleep(1.2 * (2 ** attempt))
    st.warning(f"Nepodařilo se připojit k ARES (REST): {last_err}")
    return []

def ares_search_robust(candidate: str):
    """Zkus více variant dotazu: plný název, bez diakritiky, 1–2 slova jádra."""
    queries = []
    full = (candidate or "").strip()
    queries.append(full)
    queries.append(strip_diacritics(full))

    core = norm_core(candidate)
    core_words = core.split()
    if len(core_words) >= 1:
        queries.append(core_words[0])
    if len(core_words) >= 2:
        queries.append(" ".join(core_words[:2]))

    seen = set()
    hits = []
    for q in queries:
        q = q.strip()
        if not q or q.lower() in seen:
            continue
        seen.add(q.lower())
        try:
            hits += ares_query(q, ARES_COUNT)
        except Exception:
            pass
        # deduplikace podle normalizovaného jádra
        dedup = {}
        for h in hits:
            key = norm_core(h["name"])
            if key and key not in dedup:
                dedup[key] = h
        hits = list(dedup.values())
    return hits

# ========= SIMILARITY =========
def max_similarity(candidate: str, corpus: list):
    """Vezmeme maximum ze 4 různých metrik (opatrnější)."""
    names = [h["name"] for h in corpus]
    if not names:
        return 0, None
    scorers = [fuzz.token_set_ratio, fuzz.token_sort_ratio, fuzz.partial_ratio, fuzz.QRatio]
    max_s = 0
    best_match = None
    for sc in scorers:
        match, score, idx = process.extractOne(candidate, names, scorer=sc)
        if score > max_s:
            max_s, best_match = score, match
    return max_s, best_match

# ========= NAME GENERATION =========
def generate_ai_names(keywords, style, n=10):
    prompt = (
        f"Vymysli {n} originálních názvů firmy, které NEJSOU běžnými českými slovy a nejsou generické.\n"
        f"Klíčová slova/obor: {keywords}\n"
        f"Styl: {style or 'moderní, stručné, snadno vyslovitelné'}\n"
        f"Podmínky: 1 slovo, bez diakritiky, bez právních přípon, bez slov jako group/consulting/solutions.\n"
        f"Piš jen názvy, každý na nový řádek."
    )
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.9,
    )
    text = resp.choices[0].message.content.strip()
    names = [line.strip("-• ").strip() for line in text.split("\n") if line.strip()]
    # základní filtrování
    clean = []
    for nm in names:
        core = norm_core(nm)
        if not core:
            continue
        if any(g in core.split() for g in GENERIC_WORDS):
            continue
        if not (4 <= len(core) <= 14):
            continue
        clean.append(nm[:70])
    # dedup
    seen, uniq = set(), []
    for nm in clean:
        key = norm_core(nm)
        if key not in seen:
            seen.add(key)
            uniq.append(nm)
    return uniq[:n]

def generate_safe_free_names(keywords, style, desired, free_threshold):
    """Vygeneruje kandidáty a propustí jen ty, které jsou pod zadaným prahem podobnosti."""
    results = []
    attempts = 0
    # maximálně 6 iterací nenásilně, ať zbytečně netrápíme API
    while len(results) < desired and attempts < 6:
        attempts += 1
        candidates = generate_ai_names(keywords, style, desired)
        for cand in candidates:
            hits = ares_search_robust(cand)
            score, match = max_similarity(cand, hits)
            if score < free_threshold:
                results.append({
                    "Návrh": cand,
                    "Výsledek": "✅ Volné (bez blízkých shod)",
                    "Nejbližší shoda": match,
                    "Skóre": round(score, 1)
                })
            if len(results) >= desired:
                break
    return results

# ========= UI =========
st.set_page_config(page_title="Generátor názvů + ARES kontrola", layout="centered")
st.title("🧭 Generátor názvů firem + kontrola v ARES (CZ) – SAFE režim")

with st.expander("⚙️ Nastavení"):
    mode = st.selectbox("Režim", ["Bezpečné názvy (doporučeno)", "Kreativní + kontrola (standard)"])
    n = st.slider("Počet výsledků", 5, 30, 10)
    style = st.text_input("Styl (volitelné)", "moderní, stručné, nezaměnitelné")
    free_thr = st.slider("Práh pro 'Volné' (max. podobnost %)", 55, 85, 70)
    st.caption("Čím nižší práh, tím přísnější filtr (méně návrhů projde). Doporučeno 65–75 %.")

keywords = st.text_input("Zadej obor/klíčová slova (např. právní služby, AI školení, kavárna):")

if st.button("Vygenerovat a zkontrolovat"):
    if not keywords.strip():
        st.warning("Zadej aspoň obor nebo klíčová slova.")
        st.stop()

    if mode == "Bezpečné názvy (doporučeno)":
        with st.spinner("🔒 Generuji bezpečné názvy a přísně ověřuji v ARES…"):
            results = generate_safe_free_names(keywords, style, desired=n, free_threshold=free_thr)
        if not results:
            st.error("Nepodařilo se najít dostatečný počet 'bezpečných' názvů. Zkus snížit práh nebo upřesnit klíčová slova.")
        else:
            st.success("Hotovo. Zde jsou výsledky:")
            st.dataframe(results, use_container_width=True)
    else:
        with st.spinner("🎨 Generuji kreativní názvy a ověřuji v ARES…"):
            candidates = generate_ai_names(keywords, style, n)
            rows = []
            for cand in candidates:
                hits = ares_search_robust(cand)
                score, match = max_similarity(cand, hits)
                status = "✅ Volné (bez blízkých shod)" if score < free_thr else f"⚠️ Podobné (≈{score:.0f}% k „{match}“)"
                rows.append({
                    "Návrh": cand,
                    "Výsledek": status,
                    "Nejbližší shoda": match,
                    "Skóre": round(score, 1)
                })
            st.dataframe(rows, use_container_width=True)
