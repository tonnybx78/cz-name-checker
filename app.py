import os
import requests
import streamlit as st
from difflib import SequenceMatcher
from openai import OpenAI

# ===== API KEY HANDLING =====
def _get_api_key():
    # 1) Railway/Heroku/Render – čteme z ENV
    key = os.getenv("OPENAI_API_KEY")
    if key:
        return key
    # 2) Streamlit Cloud – fallback na st.secrets
    try:
        return st.secrets["OPENAI_API_KEY"]
    except Exception:
        return None

_api_key = _get_api_key()
if not _api_key:
    st.error("Chybí OPENAI_API_KEY. Nastav v Railway → Variables, nebo vlož do .streamlit/secrets.toml.")
    st.stop()

client = OpenAI(api_key=_api_key)

# ===== ARES API SEARCH =====
def ares_search(query, limit=20):
    url = "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty"
    params = {"obchodniJmeno": query, "pocet": limit}
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code == 200:
            data = r.json()
            return [item["obchodniJmeno"] for item in data.get("ekonomickeSubjekty", [])]
    except Exception as e:
        st.warning(f"Chyba při volání ARES: {e}")
    return []

# ===== NAME SIMILARITY =====
def similarity(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio() * 100

# ===== GENERATE CANDIDATES WITH AI =====
def generate_names(keywords, style, n=10):
    prompt = f"""
    Vymysli {n} návrhů obchodních názvů pro firmu.
    Klíčová slova: {keywords}
    Styl: {style if style else "moderní, stručné, nezaměnitelné"}
    Piš pouze názvy, každý na nový řádek.
    """
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
    )
    text = resp.choices[0].message.content.strip()
    return [line.strip() for line in text.split("\n") if line.strip()]

# ===== STREAMLIT UI =====
st.title("🚀 Generátor názvů firem + kontrola v ARES (CZ)")

with st.expander("⚙️ Nastavení"):
    n = st.slider("Počet návrhů", 5, 20, 10)
    style = st.text_input("Styl (volitelné)", "moderní, stručné, nezaměnitelné")
    limit = st.slider("Kolik výsledků stáhnout z ARES", 10, 50, 20)
    threshold_high = st.slider("Hranice 'pravděpodobně zaměnitelné' (%)", 70, 100, 90)
    threshold_mid = st.slider("Hranice 'pozor – podobné' (%)", 50, 100, 80)

keywords = st.text_input("Zadej obor/klíčová slova (např. AI školení, zámečnictví, kavárna):")

if st.button("Vygenerovat a zkontrolovat") and keywords:
    st.info("Generuji návrhy...")
    candidates = generate_names(keywords, style, n)

    st.success("Hotovo. Kontroluji ARES...")
    all_hits = []
    for cand in candidates:
        hits = ares_search(cand, limit=limit)
        results = []
        for h in hits:
            sim = similarity(cand, h)
            if sim >= threshold_high:
                results.append((h, sim, "❌ Pravděpodobně zaměnitelné"))
            elif sim >= threshold_mid:
                results.append((h, sim, "⚠️ Podobné"))
        all_hits.append((cand, results))

    for cand, results in all_hits:
        st.subheader(cand)
        if not results:
            st.write("✅ Žádné podobné názvy v ARES.")
        else:
            for r in results:
                st.write(f"{r[2]} — {r[0]} ({r[1]:.1f} %)")
