import streamlit as st
import requests
import unicodedata
from rapidfuzz import fuzz, process
from openai import OpenAI
import time

# --- OpenAI init ---
client = OpenAI(api_key=st.secrets["OPENAI_API_KEY"])

# ---------- Utils ----------
def strip_diacritics(s: str) -> str:
    if not s:
        return ""
    norm = unicodedata.normalize("NFD", s)
    return "".join(ch for ch in norm if unicodedata.category(ch) != "Mn")

def normalize_name(name: str) -> str:
    if not name:
        return ""
    s = name.lower().strip()
    s = strip_diacritics(s)
    legal = ["s.r.o.", "sro", "a.s.", "as", "k.s.", "ks", "v.o.s.", "vos", "spol. s r.o.", "spol s r o"]
    for tag in legal:
        s = s.replace(tag, " ")
    generic = ["cz", "czech", "praha", "brno", "plzen", "ostrava", "group", "holding", "solutions",
               "consulting", "system", "systems", "technology", "technologies", "services", "service",
               "studio", "company", "co", "global", "international"]
    for g in generic:
        s = s.replace(f" {g} ", " ")
        if s.endswith(f" {g}"):
            s = s[:-(len(g)+1)]
        if s.startswith(f"{g} "):
            s = s[(len(g)+1):]
    out = "".join(ch for ch in s if ch.isalnum() or ch.isspace())
    out = " ".join(out.split())
    return out

# ---------- ARES REST ----------
def ares_search(name: str, max_results: int = 20):
    base = "https://ares.gov.cz/ekonomicke-subjekty-v2/ekonomicke-subjekty"
    params = {
        "obchodniJmeno": name,
        "pocet": str(max_results),
        "razeni": "obchodniJmeno@asc",
        "zdroj": "OR"
    }
    headers = {
        "Accept": "application/json",
        "User-Agent": "cz-name-checker/1.0 (Streamlit; contact: admin@example.com)"
    }

    last_err = None
    for attempt in range(4):
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
                    out.append({"oficialni_nazev": of, "ico": ico})
            return out
        except Exception as e:
            last_err = e
            time.sleep(1.5 * (2 ** attempt))
    st.warning("Nepodařilo se připojit k ARES (REST). Zkuste to znovu za chvíli.")
    return []

# ---------- Kontrola ----------
def check_exact_and_similar(candidate: str, ares_hits: list, high=90, medium=80):
    cand_norm = normalize_name(candidate)
    for item in ares_hits:
        if normalize_name(item["oficialni_nazev"]) == cand_norm:
            return "obsazeno", item["oficialni_nazev"], 100
    corpus = [x["oficialni_nazev"] for x in ares_hits]
    if corpus:
        match, score, idx = process.extractOne(candidate, corpus, scorer=fuzz.token_set_ratio)
        if score >= high:
            return "pravdepodobne_zamenitelne", match, score
        if score >= medium:
            return "pozor_podobne", match, score
    return "volne", None, None

# ---------- OpenAI ----------
def generate_names(prompt: str, n: int = 10, style: str = "moderní, stručné, nezaměnitelné"):
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.8,
        messages=[
            {"role": "system", "content": "Jsi kreativní asistent pro tvorbu názvů firem v češtině. Nepřidávej právní přípony (s.r.o., a.s.)."},
            {"role": "user", "content": f"Vymysli {n} originálních názvů firmy pro: {prompt}. Styl: {style}. Každý název na nový řádek."}
        ],
    )
    text = resp.choices[0].message.content.strip()
    names = [line.strip("-• ").strip() for line in text.split("\n") if line.strip()]
    seen, clean = set(), []
    for nm in names:
        if nm.lower() not in seen:
            seen.add(nm.lower())
            clean.append(nm[:70])
    return clean[:n]

# ---------- UI ----------
st.set_page_config(page_title="Generátor názvů + ARES kontrola", page_icon="🧭", layout="centered")
st.title("🧭 Generátor názvů firem + kontrola v ARES (CZ)")

with st.expander("Nastavení"):
    n = st.slider("Počet návrhů", 5, 30, 10, step=1)
    style = st.text_input("Styl (volitelné)", "moderní, stručné, nezaměnitelné")
    max_ares = st.slider("Kolik výsledků stáhnout z ARES", 5, 50, 20, step=5)
    high_thr = st.slider("Hranice 'pravděpodobně zaměnitelné' (%)", 85, 100, 90)
    med_thr = st.slider("Hranice 'pozor – podobné' (%)", 70, 95, 80)

prompt = st.text_input("Zadej obor/klíčová slova (např. AI školení, zámečnictví, kavárna):")

if st.button("Vygenerovat a zkontrolovat"):
    if not prompt:
        st.warning("Zadej aspoň obor nebo klíčová slova.")
        st.stop()

    with st.spinner("🔮 Generuji názvy..."):
        candidates = generate_names(prompt, n=n, style=style)

    st.success("Hotovo. Kontroluji ARES…")
    rows = []
    for cand in candidates:
        try:
            hits = ares_search(cand, max_results=max_ares)
            status, near, score = check_exact_and_similar(cand, hits, high=high_thr, medium=med_thr)
        except Exception:
            status, near, score = "chyba", None, None

        if status == "obsazeno":
            human = f"❌ Obsazeno ({near})"
        elif status == "pravdepodobne_zamenitelne":
            human = f"⚠️ Pravděpodobně zaměnitelné (≈{score}% k „{near}“)"
        elif status == "pozor_podobne":
            human = f"🟨 Pozor – podobné (≈{score}% k „{near}“)"
        elif status == "volne":
            human = "✅ Volné"
        else:
            human = "❓ Chyba při kontrole"

        rows.append({"Návrh": cand, "Výsledek": human})

    st.table(rows)
