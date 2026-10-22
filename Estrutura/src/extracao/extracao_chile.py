"""
Coleta de concorrentes no Mercado Libre Chile (mercadolibre.cl).

Para cada termo de busca (os 5 produtos da planilha), abre a listagem,
extrai os anúncios (preço em CLP, vendedor, link...) e salva em:
  - SQLite: Estrutura/data/banco_chile.db  (tabela 'produtos_chile')
  - CSV:    Estrutura/data/concorrencia_chile_AAAA-MM-DD.csv

Uso (na sua máquina, com Chrome instalado):
  pip install -r requirements.txt
  python Estrutura/src/extracao/extracao_chile.py
  python Estrutura/src/extracao/extracao_chile.py --headless --paginas 2

AVISO: os seletores CSS do Mercado Libre mudam com frequência. Se vier tudo
vazio, inspecione a página e ajuste a seção SELETORES abaixo. Respeite os
termos de uso do site e mantenha pausas entre requisições.
"""
import argparse
import re
import sqlite3
from datetime import date
from pathlib import Path
from time import sleep

import pandas as pd
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

# ----------------------------------------------------------------
# Produtos da planilha -> termo de busca e slug da URL de listagem
# ----------------------------------------------------------------
BUSCAS = {
    "organizador modular apilable": "organizador-modular-apilable",
    "fuente agua gatos": "fuente-agua-gatos",
    "compresor aire portatil recargable": "compresor-aire-portatil-recargable",
    "accesorios freidora de aire": "accesorios-freidora-aire",
    "linterna frontal recargable": "linterna-frontal-recargable",
}

BASE = "https://listado.mercadolibre.cl"
RAIZ = Path(__file__).resolve().parents[2]          # .../Estrutura
DB = RAIZ / "data" / "banco_chile.db"

# ----------------------------------------------------------------
# SELETORES (ajuste aqui se o layout mudar)
# ----------------------------------------------------------------
SEL_CARD = "li.ui-search-layout__item, div.poly-card"
SEL_TITULO = "a.poly-component__title, h2 a, h3 a"
SEL_VENDEDOR = ".poly-component__seller"
SEL_PRECO_ATUAL = ".poly-price__current .andes-money-amount__fraction"
SEL_PRECO_ANTES = "s .andes-money-amount__fraction"
SEL_PRECO_QUALQUER = ".andes-money-amount__fraction"
SEL_NOTA = ".poly-reviews__rating"
SEL_AVALIACOES = ".poly-reviews__total"
SEL_ENVIO = ".poly-component__shipping"
SEL_IMG = "img"


def texto(el, css):
    try:
        t = el.find_element(By.CSS_SELECTOR, css).text.strip()
        return t or None
    except Exception:
        return None


def atributo(el, css, attr):
    try:
        return el.find_element(By.CSS_SELECTOR, css).get_attribute(attr) or None
    except Exception:
        return None


def clp(valor):
    """'24.990' -> 24990.0 (separador de milhar no Chile é o ponto)."""
    if not valor:
        return None
    limpo = re.sub(r"[^\d]", "", valor)
    return float(limpo) if limpo else None


def inteiro(valor):
    if not valor:
        return None
    m = re.search(r"\d[\d.]*", valor)
    return int(m.group().replace(".", "")) if m else None


def extrair_card(card, termo, pagina, posicao):
    preco = texto(card, SEL_PRECO_ATUAL) or texto(card, SEL_PRECO_QUALQUER)
    nota = texto(card, SEL_NOTA)
    try:
        nota = float(nota.replace(",", ".")) if nota else None
    except ValueError:
        nota = None
    envio = texto(card, SEL_ENVIO) or ""
    return {
        "termo": termo,
        "pagina": pagina,
        "posicao": posicao,
        "produto": texto(card, SEL_TITULO) or atributo(card, SEL_TITULO, "title"),
        "vendedor": texto(card, SEL_VENDEDOR),
        "preco_clp": clp(preco),
        "preco_antes_clp": clp(texto(card, SEL_PRECO_ANTES)),
        "nota": nota,
        "avaliacoes": inteiro(texto(card, SEL_AVALIACOES)),
        "frete_gratis": "gratis" in envio.lower() or "gratis" in envio.lower().replace("á", "a"),
        "full": "full" in card.text.lower(),
        "link": atributo(card, SEL_TITULO, "href"),
        "imagem": atributo(card, SEL_IMG, "src"),
        "data_coleta": date.today().isoformat(),
    }


def coletar(driver, termo, slug, paginas, pausa):
    linhas = []
    for p in range(paginas):
        # Mercado Libre pagina com sufixo _Desde_49, _Desde_97 ... (48 itens por página)
        sufixo = "" if p == 0 else f"_Desde_{p * 48 + 1}"
        url = f"{BASE}/{slug}{sufixo}"
        print(f"  {url}")
        driver.get(url)
        sleep(pausa)
        for y in range(0, 6000, 1000):
            driver.execute_script(f"window.scrollTo(0, {y});")
            sleep(0.4)
        cards = driver.find_elements(By.CSS_SELECTOR, SEL_CARD)
        if not cards:
            print("  (nenhum anúncio encontrado: bloqueio/captcha ou seletor desatualizado)")
            break
        for i, c in enumerate(cards, start=1):
            linhas.append(extrair_card(c, termo, p + 1, i))
    return linhas


def resumo(df):
    """Mediana de preço e nº de vendedores por termo: base para a planilha."""
    g = df.dropna(subset=["preco_clp"]).groupby("termo")
    r = g["preco_clp"].agg(anuncios="count", minimo="min", mediana="median", maximo="max")
    r["vendedores"] = g["vendedor"].nunique()
    r["pct_frete_gratis"] = (g["frete_gratis"].mean() * 100).round(0)
    r["pct_full"] = (g["full"].mean() * 100).round(0)
    return r.round(0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--paginas", type=int, default=1, help="páginas por produto (48 anúncios cada)")
    ap.add_argument("--pausa", type=float, default=6.0, help="segundos de espera após abrir cada página")
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    opts = Options()
    if args.headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--lang=es-CL")
    opts.add_argument("--window-size=1400,1000")
    driver = webdriver.Chrome(options=opts)

    todas = []
    try:
        for termo, slug in BUSCAS.items():
            print(f"\n>>> {termo}")
            todas += coletar(driver, termo, slug, args.paginas, args.pausa)
            sleep(2.5)
    finally:
        driver.quit()

    if not todas:
        print("\nNada coletado.")
        return

    df = pd.DataFrame(todas)
    DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB) as conn:
        df.to_sql("produtos_chile", conn, if_exists="append", index=False)
    csv = DB.parent / f"concorrencia_chile_{date.today().isoformat()}.csv"
    df.to_csv(csv, index=False, encoding="utf-8-sig")

    print(f"\n✔ {len(df)} anúncios salvos em {DB} e {csv}\n")
    print(resumo(df).to_string())


if __name__ == "__main__":
    main()
