import streamlit as st
import requests
import xml.etree.ElementTree as ET
import unicodedata
from rapidfuzz import fuzz, process
from openai import OpenAI

client = OpenAI(api_key=st.secrets["OPENAI_API_KEY"])

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
    out = "".join(ch for ch in s if ch.isalnum() or ch.isspace())
    out = " ".join(out.split())
    return out

def ares_search(name: str, max_results: int = 20):
    url = "https://wwwinfo.mfcr.cz/cgi-bin/ares/darv_bas.cgi"
    params = {"obch_jm": name, "jazyk": "cz", "maxpoc": str(max_results), "typ_vyhledani": "full"}
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    results = []
    try:
        root = ET.fromstring(r.content)
        ns = {"are": "http://wwwinfo.mfcr.cz/ares/xml_doc/schemas/ares/ares_answer_bas/v_1.0.4"}
        for rec in root.findall(".//are:VBAS", ns):
            of = rec.findtext("are:OF", default="", namespaces=ns)
            ico = rec.findtext("are:ICO", default="", namespaces=ns)
            if of:
                results.append({"oficialni_nazev": of, "ico": ico})
    except ET.ParseError:
        pass
    return results

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

def generate_names(prompt: str, n: int = 10, style: str = "moderní, stručné, nezaměnitelné"):
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.8,
        messages=[
            {"role": "system", "content": "Jsi kreativní asistent pro tvorbu názvů firem v češtině."},
            {"role": "user", "content": f"Vymysli {n} originálních názvů firmy pro: {prompt}. Styl: {style}."}
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

st.set_page_config(page_title="Generátor názvů + ARES kontrola", page_icon="🧭", layout="centered")
st.title("🧭 Generátor názvů firem + kontrola v ARES (CZ)")

prompt = st.text_input("Zadej obor/klíčová slova:")

if st.button("Vygenerovat a zkontrolovat"):
    if not prompt:
        st.warning("Zadej aspoň obor nebo klíčová slova.")
    else:
        candidates = generate_names(prompt)
        rows = []
        for cand in candidates:
            hits = ares_search(cand)
            status, near, score = check_exact_and_similar(cand, hits)
            rows.append({"Návrh": cand, "Výsledek": status})
        st.dataframe(rows)
