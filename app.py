import os
import re
import requests
import unicodedata
import streamlit as st
from rapidfuzz import fuzz, process
from openai import OpenAI

# ===== API KEY HANDLING =====
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

# ===== NORMALIZATION =====
LEGAL_SUFFIXES = [
    "s.r.o.", "sro", "a.s.", "as", "k.s.", "ks", "v.o.s.", "vos", "spol. s r.o.", "spol s r o"
]
GENERIC_WORDS = [
    "cz","czech","praha","brno","plzen","ostrava","group","holding","solutions","consulting",
    "system","systems","technology","technologies","services","service","studio","company","co",
    "global","international","advisory","advisers","adviser"
]

def strip_diacritics(s: str) -> str:
    norm = unicodedata.normalize("NFD", s)
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

# ===== ARES SEARCH (v2 REST, zdroj OR) =====
def ares_query(obchodni_jmeno: str, count: int = 50):
    base = "https://ares.gov.cz/ekonomicke-subjekty-v2/ekonomicke-subjekty"
    params = {
        "obchodniJmeno": obchodni_jmeno,
        "pocet": str(count),
        "razeni": "obchodniJmeno@asc",
        "zdroj": "OR"
    }
    headers = {
        "Accept": "application/json",
        "User-Agent": "cz-name-checker/1.1 (+contact: owner@example.com)"
    }
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

def ares_search_robust(candidate: str, count: int = 50):
    queries = []
    full = candidate.strip()
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
            hits += ares_query(q, count)
        except Exception:
            pass
        dedup = {}
        for h in hits:
            key = norm_core(h["name"])
            if key not in dedup:
                dedup[key] = h
        hits = list(dedup.values())
    return hits

# ===== SIMILARITY (RapidFuzz token set) =====
def best_similarity(candidate: str, corpus: list):
    names = [h["name"] for h in corpus]
    if not names:
        return None, 0
    match, score, idx = process.extractOne(candidate, names, scorer=fuzz.token_set_ratio)
    return match, score

# ===== GENERATE NAMES =====
def generate_names(keywords, style, n=10):
    prompt = (
        f"Vymysli {n} originálních názvů firmy pro: {keywords}. "
        f"Styl: {style or 'moderní, stručné, nezaměnitelné'}. "
        f"Nepřidávej právní přípony (s.r.o., a.s.). Každý název na nový řádek."
    )
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
    )
    text = resp.choices[0].message.content.strip()
    names = [line.strip("-• ").strip() for line in text.split("\n") if line.strip()]
    seen, clean = set(), []
    for nm in names:
        key = norm_core(nm)
        if key and key not in seen:
            seen.add(key)
            clean.append(nm[:70])
    return clean[:n]

# ===== UI =====
st.set_page_config(page_title="Generátor názvů + ARES kontrola", layout="centered")
st.title("🧭 Generátor názvů firem + kontrola v ARES (CZ)")

with st.expander("Nastavení"):
    n = st.slider("Počet návrhů", 5, 30, 10)
    style = st.text_input("Styl (volitelné)", "moderní, stručné, nezaměnitelné")
    max_ares = st.slider("Kolik výsledků stáhnout z ARES", 10, 100, 50, step=10)
    high_thr = st.slider("Hranice 'pravděpodobně zaměnitelné' (%)", 80, 100, 92)
    med_thr = st.slider("Hranice 'pozor – podobné' (%)", 70, 95, 85)

keywords = st.text_input("Zadej obor/klíčová slova (např. Axelrod advisory, AI školení, kavárna):")

if st.button("Vygenerovat a zkontrolovat"):
    if not keywords.strip():
        st.warning("Zadej aspoň obor nebo klíčová slova.")
        st.stop()
    with st.spinner("🔮 Generuji návrhy..."):
        candidates = generate_names(keywords, style, n)
    st.success("Hotovo. Kontroluji ARES…")
    results = []
    for cand in candidates:
        hits = ares_search_robust(cand, count=max_ares)
        match, score = best_similarity(cand, hits)
        if score >= high_thr:
            status = f"❌ Pravděpodobně zaměnitelné (≈{score:.0f}% k „{match}“)"
        elif score >= med_thr:
            status = f"🟨 Pozor – podobné (≈{score:.0f}% k „{match}“)"
        else:
            status = "✅ Volné (bez blízkých shod)"
        results.append({"Návrh": cand, "Výsledek": status})
    st.subheader("Výsledky")
    st.dataframe(results, use_container_width=True)
    st.caption("Pozn.: Kontrola je heuristická. Finální posouzení dělá rejstříkový soud.")
