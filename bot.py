"""
Bot de mercado para Discord: aberturas de mercado, relatórios de manhã e à noite,
notícias filtradas, calendário económico, alertas nos ranges DMT, movimentos bruscos
e funding extremo. Corre no GitHub Actions a cada 15 minutos.
"""
import datetime as dt
import hashlib
import json
import os
import sys
import time
from zoneinfo import ZoneInfo

import feedparser
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
MODO = (os.environ.get("MODO") or "normal").strip().lower()
UTC = dt.timezone.utc
HTTP = requests.Session()
HTTP.headers["User-Agent"] = "bot-trading-dmt/1.0"


# ------------------------------------------------------------------ utilidades
def ler_json(nome, padrao):
    caminho = os.path.join(BASE, nome)
    if not os.path.exists(caminho):
        return padrao
    with open(caminho, encoding="utf-8") as f:
        return json.load(f)


def gravar_json(nome, dados):
    with open(os.path.join(BASE, nome), "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)


CFG = ler_json("config.json", {})
NIVEIS = ler_json("niveis.json", {"ranges": []})
ESTADO = ler_json("state.json", {})
TZ = ZoneInfo(CFG.get("fuso", "Europe/Lisbon"))


def fmt(x, casas=None):
    if x is None:
        return "?"
    if casas is None:
        a = abs(x)
        casas = 0 if a >= 10000 else 2 if a >= 100 else 3 if a >= 1 else 6
    s = f"{x:,.{casas}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def pct(x, casas=2):
    if x is None:
        return "?"
    return ("+" if x > 0 else "") + fmt(x, casas) + "%"


def hora_local(t):
    return t.astimezone(TZ).strftime("%H:%M")


def log(*a):
    print(*a, flush=True)


# ------------------------------------------------------------------ Discord
def enviar(texto):
    partes, atual = [], ""
    for linha in texto.split("\n"):
        if len(atual) + len(linha) + 1 > 1900:
            partes.append(atual)
            atual = ""
        atual += linha + "\n"
    if atual.strip():
        partes.append(atual)
    for p in partes:
        if not WEBHOOK:
            log("[sem webhook]\n" + p)
            continue
        for _ in range(3):
            r = HTTP.post(WEBHOOK, json={"content": p, "allowed_mentions": {"parse": []}}, timeout=15)
            if r.status_code == 429:
                time.sleep(float(r.json().get("retry_after", 2)))
                continue
            if r.status_code >= 400:
                log("Erro Discord", r.status_code, r.text[:200])
            break
        time.sleep(0.6)


# ------------------------------------------------------------------ dados de mercado
def binance(caminho, params):
    r = HTTP.get("https://data-api.binance.vision/api/v3/" + caminho, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def velas(simbolo, intervalo, limite):
    dados = binance("klines", {"symbol": simbolo, "interval": intervalo, "limit": limite})
    return [{"abre": k[0] / 1000, "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
             "c": float(k[4]), "v": float(k[5]), "fecha": k[6] / 1000} for k in dados]


def ticker24(simbolo):
    return binance("ticker/24hr", {"symbol": simbolo})


_HL = None


def hyperliquid():
    """Funding e open interest dos perpétuos (Hyperliquid)."""
    global _HL
    if _HL is None:
        r = HTTP.post("https://api.hyperliquid.xyz/info", json={"type": "metaAndAssetCtxs"}, timeout=15)
        r.raise_for_status()
        meta, ctxs = r.json()
        _HL = {}
        for a, c in zip(meta["universe"], ctxs):
            try:
                _HL[a["name"]] = {
                    "funding8h": float(c["funding"]) * 8 * 100,
                    "oi_usd": float(c["openInterest"]) * float(c["markPx"]),
                }
            except (KeyError, TypeError, ValueError):
                pass
    return _HL


def ema(valores, n):
    k = 2 / (n + 1)
    e = valores[0]
    for v in valores[1:]:
        e = v * k + e * (1 - k)
    return e


def tendencia(simbolo, intervalo):
    vs = velas(simbolo, intervalo, 250)
    fechos = [v["c"] for v in vs]
    preco, e50, e200 = fechos[-1], ema(fechos, 50), ema(fechos, 200)
    if preco > e50 > e200:
        return "alta"
    if preco < e50 < e200:
        return "baixa"
    return "mista"


def simbolo_de(moeda):
    return moeda.upper() + CFG.get("cotacao", "USDT")


# ------------------------------------------------------------------ tempo
def cruzou(hora, fuso, dias, ultima, agora):
    """True se a hora local `hora` (HH:MM) no fuso `fuso` ocorreu entre `ultima` e `agora`."""
    tz = ZoneInfo(fuso)
    hh, mm = map(int, hora.split(":"))
    d = ultima.astimezone(tz).date()
    fim = agora.astimezone(tz).date()
    while d <= fim:
        t = dt.datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz)
        if (dias is None or t.weekday() in dias) and ultima < t <= agora:
            return t
        d += dt.timedelta(days=1)
    return None


# ------------------------------------------------------------------ aberturas de mercado
def aberturas(ultima, agora):
    for m in CFG.get("mercados", []):
        t = cruzou(m["hora"], m["fuso"], [0, 1, 2, 3, 4], ultima, agora)
        if not t:
            continue
        extra = ""
        try:
            tk = ticker24(simbolo_de("BTC"))
            extra = f"\nBTC a {fmt(float(tk['lastPrice']))} ({pct(float(tk['priceChangePercent']))} em 24h)."
        except Exception as e:
            log("aberturas/ticker", e)
        enviar(f"🔔 **Abertura de {m['nome']}** ({hora_local(t)} em Portugal). {m.get('nota', '')}{extra}")


# ------------------------------------------------------------------ calendário económico
def calendario(agora):
    cal = ESTADO.setdefault("calendario", {"obtido": None, "eventos": []})
    obtido = dt.datetime.fromisoformat(cal["obtido"]) if cal.get("obtido") else None
    if obtido is None or agora - obtido > dt.timedelta(hours=6):
        try:
            r = HTTP.get(CFG["calendario"]["url"], timeout=20)
            r.raise_for_status()
            moedas = set(CFG["calendario"].get("moedas", ["USD"]))
            impactos = set(CFG["calendario"].get("impactos", ["High"]))
            eventos = []
            for e in r.json():
                if e.get("country") in moedas and e.get("impact") in impactos and e.get("date"):
                    eventos.append({"titulo": e.get("title", "?"), "moeda": e.get("country"),
                                    "data": dt.datetime.fromisoformat(e["date"]).astimezone(UTC).isoformat(),
                                    "previsao": e.get("forecast") or "", "anterior": e.get("previous") or ""})
            cal["eventos"] = eventos
            cal["obtido"] = agora.isoformat()
        except Exception as e:
            log("calendario", e)
    return [dict(e, data=dt.datetime.fromisoformat(e["data"])) for e in cal.get("eventos", [])]


def avisos_calendario(agora):
    minutos = CFG["calendario"].get("aviso_min", 30)
    avisados = ESTADO.setdefault("eventos_avisados", [])
    for e in calendario(agora):
        chave = e["titulo"] + e["data"].isoformat()
        falta = (e["data"] - agora).total_seconds() / 60
        if 0 < falta <= minutos and chave not in avisados:
            det = ", ".join(x for x in [f"previsão {e['previsao']}" if e["previsao"] else "",
                                         f"anterior {e['anterior']}" if e["anterior"] else ""] if x)
            enviar(f"⚠️ **Daqui a {round(falta)} min: {e['moeda']} {e['titulo']}** ({hora_local(e['data'])})"
                   + (f"\n{det}." if det else "")
                   + "\nPasso 1 da tua verificação: não abres trades em cima desta notícia.")
            avisados.append(chave)
    ESTADO["eventos_avisados"] = avisados[-200:]


def eventos_hoje(agora):
    hoje = agora.astimezone(TZ).date()
    evs = sorted((e for e in calendario(agora) if e["data"].astimezone(TZ).date() == hoje), key=lambda e: e["data"])
    if not evs:
        return "Sem notícias de alto impacto hoje."
    return "\n".join(f"• {hora_local(e['data'])} {e['moeda']} {e['titulo']}" for e in evs)


# ------------------------------------------------------------------ notícias
def noticias():
    n = CFG.get("noticias", {})
    vistos = set(ESTADO.get("noticias_vistas", []))
    primeira = not ESTADO.get("noticias_iniciadas")
    palavras = [p.lower() for p in n.get("palavras", [])] + [m.lower() for m in CFG.get("moedas", [])]
    novas = []
    for feed in n.get("feeds", []):
        try:
            r = HTTP.get(feed["url"], timeout=20)
            r.raise_for_status()
            for item in feedparser.parse(r.content).entries[:30]:
                chave = hashlib.sha1((item.get("id") or item.get("link") or item.get("title", "")).encode()).hexdigest()[:16]
                if chave in vistos:
                    continue
                vistos.add(chave)
                titulo = item.get("title", "").strip()
                t = " " + titulo.lower() + " "
                if any(p in t for p in palavras):
                    novas.append((feed["nome"], titulo, item.get("link", "")))
        except Exception as e:
            log("noticias", feed.get("nome"), e)
    ESTADO["noticias_vistas"] = list(vistos)[-600:]
    ESTADO["noticias_iniciadas"] = True
    if primeira:
        return  # primeira execução: marca o que já existe, sem inundar o canal
    for fonte, titulo, link in novas[: n.get("max_por_execucao", 5)]:
        enviar(f"📰 **{titulo}**\n{fonte}: <{link}>")


# ------------------------------------------------------------------ movimentos e funding
def movimentos_e_funding(agora):
    alertas = ESTADO.setdefault("alertas_tempo", {})
    lim_mov = CFG.get("alertas", {}).get("movimento_1h_pct", 3)
    lim_fund = CFG.get("alertas", {}).get("funding_8h_pct", 0.05)
    for m in CFG.get("moedas", []):
        try:
            vs = velas(simbolo_de(m), "15m", 5)
            var = (vs[-1]["c"] / vs[0]["o"] - 1) * 100
            chave = f"mov-{m}"
            ult = alertas.get(chave)
            if abs(var) >= lim_mov and (not ult or agora - dt.datetime.fromisoformat(ult) > dt.timedelta(hours=1)):
                enviar(f"{'🚀' if var > 0 else '🔻'} **{m} mexeu {pct(var)} na última hora**, está a {fmt(vs[-1]['c'])}.")
                alertas[chave] = agora.isoformat()
        except Exception as e:
            log("movimento", m, e)
        try:
            f = hyperliquid().get(m.upper())
            chave = f"fund-{m}"
            ult = alertas.get(chave)
            if f and abs(f["funding8h"]) >= lim_fund and (not ult or agora - dt.datetime.fromisoformat(ult) > dt.timedelta(hours=8)):
                lado = "longs a pagar aos shorts: mercado carregado de compras" if f["funding8h"] > 0 else "shorts a pagar aos longs: mercado carregado de vendas"
                enviar(f"💸 **Funding extremo em {m}: {fmt(f['funding8h'], 4)}% por 8h** ({lado}). Um lado apertado costuma acabar em limpeza de stops.")
                alertas[chave] = agora.isoformat()
        except Exception as e:
            log("funding", m, e)


# ------------------------------------------------------------------ ranges DMT
def chave_range(r):
    base = json.dumps({k: r.get(k) for k in ("par", "topo", "fundo", "dl", "lado")}, sort_keys=True)
    return r.get("id", "range") + "-" + hashlib.sha1(base.encode()).hexdigest()[:8]


def novo_estado(prev, low, close, preco, linha, dl, zona, meio):
    """Lógica para o lado do fundo (long). Para o topo, os preços chegam negados."""
    if prev == "invalidado":
        return prev
    if low <= dl:
        return "invalidado"
    if prev in ("desvio", "reentrou"):
        if prev == "reentrou" and preco >= meio:
            return "fora"
        return "reentrou" if close > linha else "desvio"
    if low < linha:
        return "desvio"
    if preco <= zona:
        return "zona"
    return "fora"


def ranges_dmt(ultima):
    estados = ESTADO.setdefault("ranges", {})
    zona_pct = CFG.get("alertas", {}).get("zona_toque_pct", 15)
    ativos = [r for r in NIVEIS.get("ranges", []) if r.get("ativo")]
    vivos = set()
    for r in ativos:
        try:
            topo, fundo, dl = float(r["topo"]), float(r["fundo"]), float(r.get("dl", 30))
            if topo <= fundo:
                continue
            k = chave_range(r)
            vivos.add(k)
            vs = velas(r["par"], "15m", 12)
            recentes = [v for v in vs if v["fecha"] > ultima.timestamp()] or vs[-1:]
            fechadas = [v for v in vs if v["fecha"] <= time.time()]
            close = fechadas[-1]["c"] if fechadas else vs[-1]["c"]
            preco = vs[-1]["c"]
            hi, lo = max(v["h"] for v in recentes), min(v["l"] for v in recentes)
            h = topo - fundo
            meio = fundo + h / 2
            lados = []
            if r.get("lado", "ambos") in ("long", "ambos"):
                lados.append(("fundo", lo, close, preco, fundo, fundo - h * dl / 100, fundo + h * zona_pct / 100, meio, 1))
            if r.get("lado", "ambos") in ("short", "ambos"):
                lados.append(("topo", -hi, -close, -preco, -topo, -(topo + h * dl / 100), -(topo - h * zona_pct / 100), -meio, -1))
            for nome, l, c, p, linha, dlv, zona, md, sinal in lados:
                ek = f"{k}-{nome}"
                prev = estados.get(ek, "fora")
                novo = novo_estado(prev, l, c, p, linha, dlv, zona, md)
                estados[ek] = novo
                if novo == prev or novo == "fora":
                    continue
                par, nota = r["par"], (f" ({r['nota']})" if r.get("nota") else "")
                lado_txt = "fundo" if sinal == 1 else "topo"
                if novo == "zona":
                    enviar(f"🎯 **{par}{nota}: preço a chegar ao {lado_txt} do range** ({fmt(abs(linha))}). Prepara a verificação.")
                elif novo == "desvio":
                    desvio = (linha - l) / h * 100
                    enviar(f"⚡ **{par}{nota}: desvio para lá do {lado_txt}**, {fmt(desvio, 1)}% da altura do range (DL a {fmt(dl, 0)}%). "
                           f"Espera o fecho de volta para dentro e o BOS.")
                elif novo == "reentrou":
                    enviar(f"✅ **{par}{nota}: fechou de volta dentro do range** depois do desvio no {lado_txt}. "
                           f"Agora procura o BOS no timeframe menor.")
                elif novo == "invalidado":
                    enviar(f"❌ **{par}{nota}: passou a DL do {lado_txt}.** Pelo teu modelo, o range partiu. "
                           f"Atualiza ou desativa este range no niveis.json.")
        except Exception as e:
            log("range", r.get("id"), e)
    for k in list(estados):
        if not any(k.startswith(v) for v in vivos):
            del estados[k]


def estado_ranges_texto():
    linhas = []
    for r in NIVEIS.get("ranges", []):
        if not r.get("ativo"):
            continue
        try:
            topo, fundo = float(r["topo"]), float(r["fundo"])
            preco = float(ticker24(r["par"])["lastPrice"])
            pos = (preco - fundo) / (topo - fundo) * 100
            sit = "acima do topo" if pos > 100 else "abaixo do fundo" if pos < 0 else f"a {fmt(pos, 0)}% da altura"
            linhas.append(f"• {r['par']}{' (' + r['nota'] + ')' if r.get('nota') else ''}: {fmt(preco)}, {sit} "
                          f"(fundo {fmt(fundo)}, topo {fmt(topo)})")
        except Exception as e:
            log("estado_ranges", r.get("id"), e)
    return "\n".join(linhas)


# ------------------------------------------------------------------ relatório
def bloco_moeda(m, semanal=False):
    s = simbolo_de(m)
    tk = ticker24(s)
    preco = float(tk["lastPrice"])
    alto, baixo = float(tk["highPrice"]), float(tk["lowPrice"])
    pos24 = (preco - baixo) / (alto - baixo) * 100 if alto > baixo else 50
    dias = velas(s, "1d", 8)
    var7 = (preco / dias[0]["o"] - 1) * 100
    alto7, baixo7 = max(v["h"] for v in dias[-7:]), min(v["l"] for v in dias[-7:])
    linhas = [f"**{m}** {fmt(preco)}  ({pct(float(tk['priceChangePercent']))} 24h, {pct(var7)} 7d)"]
    try:
        linhas.append(f"Tendência: 4h {tendencia(s, '4h')}, diário {tendencia(s, '1d')}")
    except Exception as e:
        log("tendencia", m, e)
    linhas.append(f"24h: mín {fmt(baixo)}, máx {fmt(alto)}, preço a {fmt(pos24, 0)}% do intervalo")
    linhas.append(f"7 dias: mín {fmt(baixo7)}, máx {fmt(alto7)}")
    try:
        f = hyperliquid().get(m.upper())
        if f:
            linhas.append(f"Funding {fmt(f['funding8h'], 4)}% por 8h, open interest {fmt(f['oi_usd'] / 1e6, 0)} M$")
    except Exception as e:
        log("hyperliquid", e)
    return "\n".join(linhas)


def relatorio(nome, agora):
    partes = [f"📊 **Relatório da {nome}**, {agora.astimezone(TZ).strftime('%d/%m %H:%M')}"]
    for m in CFG.get("moedas", []):
        try:
            partes.append(bloco_moeda(m))
        except Exception as e:
            log("relatorio", m, e)
            partes.append(f"**{m}**: sem dados agora.")
    partes.append("**Notícias de alto impacto hoje**\n" + eventos_hoje(agora))
    rg = estado_ranges_texto()
    if rg:
        partes.append("**Os teus ranges**\n" + rg)
    if nome == "noite" and agora.astimezone(TZ).weekday() == 6:
        partes.append("🗓️ Fim de semana: revê os ranges no niveis.json e os trades da semana no teu diário.")
    enviar("\n\n".join(partes))


def relatorios(ultima, agora):
    for nome, hora in CFG.get("relatorios", {}).items():
        if cruzou(hora, CFG.get("fuso", "Europe/Lisbon"), None, ultima, agora):
            relatorio(nome, agora)


# ------------------------------------------------------------------ principal
def main():
    agora = dt.datetime.now(UTC)
    ultima = dt.datetime.fromisoformat(ESTADO["ultima_execucao"]) if ESTADO.get("ultima_execucao") else agora - dt.timedelta(minutes=15)
    ultima = max(ultima, agora - dt.timedelta(minutes=90))  # depois de uma paragem longa, não repete o passado

    if MODO == "teste":
        enviar("✅ O bot está ligado a este canal. A seguir vem um relatório de teste.")
        relatorio("manhã" if agora.astimezone(TZ).hour < 15 else "noite", agora)
    elif MODO == "relatorio":
        relatorio("manhã" if agora.astimezone(TZ).hour < 15 else "noite", agora)

    for nome, f in [("aberturas", lambda: aberturas(ultima, agora)),
                    ("relatorios", lambda: relatorios(ultima, agora)),
                    ("calendario", lambda: avisos_calendario(agora)),
                    ("noticias", noticias),
                    ("movimentos", lambda: movimentos_e_funding(agora)),
                    ("ranges", lambda: ranges_dmt(ultima))]:
        try:
            f()
        except Exception as e:
            log("falhou", nome, e)

    ESTADO["ultima_execucao"] = agora.isoformat()
    gravar_json("state.json", ESTADO)


if __name__ == "__main__":
    main()
