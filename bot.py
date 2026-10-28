"""
Bot de mercado para Discord: aberturas de mercado com a liquidez da sessão anterior,
relatórios de manhã e à noite, notícias filtradas, calendário económico, varrimentos de
máximos e mínimos, ranges candidatos, open interest, movimentos bruscos, funding extremo
e alertas nos ranges DMT. Corre no GitHub Actions a cada 15 minutos.
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
                    "preco": float(c["markPx"]),
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
def maximo_minimo(simbolo, inicio, fim):
    vs = [v for v in velas(simbolo, "15m", 60) if v["abre"] >= inicio.timestamp() and v["fecha"] <= fim.timestamp() + 1]
    if not vs:
        return None
    return max(v["h"] for v in vs), min(v["l"] for v in vs)


def aberturas(ultima, agora):
    for m in CFG.get("mercados", []):
        t = cruzou(m["hora"], m["fuso"], [0, 1, 2, 3, 4], ultima, agora)
        if not t:
            continue
        linhas = [f"🔔 **Abertura de {m['nome']}** ({hora_local(t)} em Portugal). {m.get('nota', '')}"]
        try:
            tk = ticker24(simbolo_de("BTC"))
            linhas.append(f"BTC a {fmt(float(tk['lastPrice']))} ({pct(float(tk['priceChangePercent']))} em 24h).")
        except Exception as e:
            log("aberturas/ticker", e)
        ant = m.get("sessao_anterior")
        if ant:
            tz = ZoneInfo(ant["fuso"])
            d = t.astimezone(tz).date()
            hh, mm = map(int, ant["hora"].split(":"))
            inicio = dt.datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz)
            niveis = []
            for moeda in CFG.get("liquidez", {}).get("moedas", []):
                try:
                    mm_ = maximo_minimo(simbolo_de(moeda), inicio, t)
                    if not mm_:
                        continue
                    alto, baixo = mm_
                    niveis.append(f"• {moeda}: máximo {fmt(alto)}, mínimo {fmt(baixo)}")
                    juntar_niveis(moeda, [
                        {"nome": f"máximo da sessão {ant['rotulo']}", "preco": alto, "tipo": "max"},
                        {"nome": f"mínimo da sessão {ant['rotulo']}", "preco": baixo, "tipo": "min"},
                    ])
                except Exception as e:
                    log("sessao_anterior", moeda, e)
            if niveis:
                linhas.append(f"**Liquidez da sessão {ant['rotulo']}** (stops acumulados acima e abaixo):")
                linhas.extend(niveis)
        enviar("\n".join(linhas))


# ------------------------------------------------------------------ calendário económico
def calendario(agora):
    cal = ESTADO.setdefault("calendario", {"obtido": None, "eventos": []})
    obtido = dt.datetime.fromisoformat(cal["obtido"]) if cal.get("obtido") else None
    if obtido is None or agora - obtido > dt.timedelta(hours=6):
        moedas = set(CFG["calendario"].get("moedas", ["USD"]))
        impactos = set(CFG["calendario"].get("impactos", ["High"]))
        eventos, vistos, algum = [], set(), False
        for url in [CFG["calendario"]["url"]] + CFG["calendario"].get("urls_extra", []):
            try:
                r = HTTP.get(url, timeout=20)
                r.raise_for_status()
                algum = True
                for e in r.json():
                    if e.get("country") in moedas and e.get("impact") in impactos and e.get("date"):
                        d = dt.datetime.fromisoformat(e["date"]).astimezone(UTC).isoformat()
                        k = e.get("title", "?") + d
                        if k in vistos:
                            continue
                        vistos.add(k)
                        eventos.append({"titulo": e.get("title", "?"), "moeda": e.get("country"), "data": d,
                                        "previsao": e.get("forecast") or "", "anterior": e.get("previous") or ""})
            except Exception as e:
                log("calendario", url, e)
        if algum:
            cal["eventos"] = eventos
            cal["obtido"] = agora.isoformat()
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
    return "\n".join(f"• {hora_local(e['data'])} {e['moeda']} {e['titulo']}" + (" (já saiu)" if e["data"] <= agora else "")
                     for e in evs)


def eventos_proximos(agora, dias=7):
    fim = agora + dt.timedelta(days=dias)
    evs = sorted((e for e in calendario(agora) if agora < e["data"] <= fim), key=lambda e: e["data"])
    if not evs:
        return "Sem notícias de alto impacto nos próximos dias (ou o calendário da próxima semana ainda não saiu)."
    dias_pt = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
    return "\n".join(f"• {dias_pt[e['data'].astimezone(TZ).weekday()]} {e['data'].astimezone(TZ).strftime('%d/%m %H:%M')} "
                     f"{e['moeda']} {e['titulo']}" for e in evs)


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


# ------------------------------------------------------------------ liquidez (dias, semanas, sessões)
def juntar_niveis(moeda, novos):
    liq = ESTADO.setdefault("liquidez", {})
    lista = liq.setdefault("niveis", {}).setdefault(moeda, [])
    nomes = {n["nome"] for n in novos}
    lista[:] = [n for n in lista if n["nome"] not in nomes] + novos


def atualizar_niveis_dia(agora):
    liq = ESTADO.setdefault("liquidez", {})
    hoje = agora.date().isoformat()
    if liq.get("dia") == hoje:
        return
    liq["dia"] = hoje
    liq["niveis"] = {}
    for m in CFG.get("liquidez", {}).get("moedas", []):
        try:
            s_ = simbolo_de(m)
            ontem = velas(s_, "1d", 2)[-2]
            semana = velas(s_, "1w", 2)[-2]
            juntar_niveis(m, [
                {"nome": "máximo de ontem", "preco": ontem["h"], "tipo": "max"},
                {"nome": "mínimo de ontem", "preco": ontem["l"], "tipo": "min"},
                {"nome": "máximo da semana passada", "preco": semana["h"], "tipo": "max"},
                {"nome": "mínimo da semana passada", "preco": semana["l"], "tipo": "min"},
            ])
        except Exception as e:
            log("niveis_dia", m, e)
    limite = (agora - dt.timedelta(days=8)).date().isoformat()
    liq["varridos"] = {k: v for k, v in liq.get("varridos", {}).items() if v >= limite}


def varrimentos(ultima):
    liq = ESTADO.get("liquidez", {})
    varr = liq.setdefault("varridos", {})
    hoje = dt.datetime.now(UTC).date().isoformat()
    for m, niveis in liq.get("niveis", {}).items():
        try:
            vs = velas(simbolo_de(m), "15m", 12)
        except Exception as e:
            log("varrimentos", m, e)
            continue
        fechadas = [v for v in vs if ultima.timestamp() < v["fecha"] <= time.time()]
        for n in niveis:
            k = f"{m}|{n['nome']}|{n['preco']}"
            if k in varr:
                continue
            for v in fechadas:
                if n["tipo"] == "max":
                    if v["c"] > n["preco"]:
                        varr[k] = hoje  # rompeu e fechou acima: deixa de vigiar, sem alerta
                        break
                    if v["h"] > n["preco"]:
                        enviar(f"💧 **{m} varreu o {n['nome']}** ({fmt(n['preco'])}) e fechou de volta abaixo, a {fmt(v['c'])}. "
                               f"Os stops acima foram apanhados: atenção a uma reação para baixo.")
                        varr[k] = hoje
                        break
                else:
                    if v["c"] < n["preco"]:
                        varr[k] = hoje
                        break
                    if v["l"] < n["preco"]:
                        enviar(f"💧 **{m} varreu o {n['nome']}** ({fmt(n['preco'])}) e fechou de volta acima, a {fmt(v['c'])}. "
                               f"Os stops abaixo foram apanhados: atenção a uma reação para cima.")
                        varr[k] = hoje
                        break


def niveis_texto(moeda, preco):
    niveis = ESTADO.get("liquidez", {}).get("niveis", {}).get(moeda, [])
    varr = ESTADO.get("liquidez", {}).get("varridos", {})
    if not niveis:
        return ""
    partes = []
    for n in sorted(niveis, key=lambda n: -n["preco"]):
        dist = (n["preco"] / preco - 1) * 100
        feito = f"{moeda}|{n['nome']}|{n['preco']}" in varr
        partes.append(f"{n['nome']} {fmt(n['preco'])} ({pct(dist)}){' já tocado' if feito else ''}")
    return "Liquidez: " + "; ".join(partes)


# ------------------------------------------------------------------ ranges candidatos
def pivos(vs, k=3):
    altos, baixos = [], []
    for i in range(k, len(vs) - k):
        janela = vs[i - k:i + k + 1]
        if vs[i]["h"] == max(v["h"] for v in janela):
            altos.append(i)
        if vs[i]["l"] == min(v["l"] for v in janela):
            baixos.append(i)
    return altos, baixos


def espacar(idx, minimo):
    out = []
    for i in idx:
        if not out or i - out[-1] >= minimo:
            out.append(i)
    return out


def procurar_range(vs, tf):
    sc = CFG.get("scanner", {})
    amin = sc.get("altura_min_pct", {}).get(tf, 1.5)
    amax = sc.get("altura_max_pct", {}).get(tf, 15)
    tol_pct = sc.get("tolerancia_pct", 12)
    tmin = sc.get("toques_min", 2)
    atual = vs[-1]["c"]
    base = vs[:-1]
    melhor = None
    for W in range(30, len(base) + 1, 5):
        jan = base[-W:]
        topo, fundo = max(v["h"] for v in jan), min(v["l"] for v in jan)
        h = topo - fundo
        if h <= 0 or not (amin <= h / fundo * 100 <= amax) or not (fundo <= atual <= topo):
            continue
        ph, pl = pivos(jan)
        tol = h * tol_pct / 100
        th = espacar([i for i in ph if jan[i]["h"] >= topo - tol], 5)
        tl = espacar([i for i in pl if jan[i]["l"] <= fundo + tol], 5)
        if len(th) < tmin or len(tl) < tmin:
            continue
        terco = W // 3
        m1 = sum(v["c"] for v in jan[:terco]) / terco
        m3 = sum(v["c"] for v in jan[-terco:]) / terco
        if abs(m1 - m3) > 0.35 * h:
            continue
        pontos = len(th) + len(tl)
        if melhor is None or pontos > melhor["toques"] or (pontos == melhor["toques"] and W > melhor["velas"]):
            melhor = {"topo": topo, "fundo": fundo, "velas": W, "toques": pontos, "cima": len(th), "baixo": len(tl),
                      "pos": (atual - fundo) / h * 100, "altura": h / fundo * 100}
    return melhor


def texto_candidato(m, tf, c):
    return (f"{m} {tf}: {fmt(c['fundo'])} a {fmt(c['topo'])} ({fmt(c['altura'], 1)}% de altura, {c['velas']} velas), "
            f"toques {c['cima']} em cima e {c['baixo']} em baixo, preço a {fmt(c['pos'], 0)}% da altura")


def scanner(agora, alertar=True):
    sc = CFG.get("scanner", {})
    guardados = ESTADO.setdefault("candidatos", {})
    resultado = []
    for m in sc.get("moedas", []):
        for tf, n in sc.get("timeframes", {}).items():
            k = f"{m}-{tf}"
            try:
                c = procurar_range(velas(simbolo_de(m), tf, n), tf)
            except Exception as e:
                log("scanner", k, e)
                continue
            if not c:
                if alertar:
                    guardados.pop(k, None)
                continue
            resultado.append(texto_candidato(m, tf, c))
            prev = guardados.get(k)
            h = c["topo"] - c["fundo"]
            novo = not prev or abs(prev["topo"] - c["topo"]) > 0.15 * h or abs(prev["fundo"] - c["fundo"]) > 0.15 * h
            if novo and alertar:
                enviar(f"🧭 **Range candidato: {texto_candidato(m, tf, c)}.**\n"
                       f"As linhas são os extremos da zona, não o primeiro toque: confirma no gráfico antes de o seguires.")
            if novo and alertar:
                guardados[k] = {"topo": c["topo"], "fundo": c["fundo"]}
    return resultado


# ------------------------------------------------------------------ open interest
def open_interest(agora):
    lim = CFG.get("alertas", {}).get("oi_1h_pct", 5)
    hist = ESTADO.setdefault("oi_hist", {})
    alertas = ESTADO.setdefault("alertas_tempo", {})
    try:
        hl = hyperliquid()
    except Exception as e:
        log("oi", e)
        return
    ts = agora.timestamp()
    for m in CFG.get("moedas", []):
        f = hl.get(m.upper())
        if not f:
            continue
        pontos = [p for p in hist.get(m, []) if ts - p[0] <= 30 * 3600]
        antes = [p for p in pontos if 55 * 60 <= ts - p[0] <= 90 * 60]
        pontos.append([ts, f["oi_usd"], f["preco"]])
        hist[m] = pontos
        if not antes:
            continue
        p0 = antes[-1]
        var_oi = (f["oi_usd"] / p0[1] - 1) * 100
        var_px = (f["preco"] / p0[2] - 1) * 100
        tipo = None
        if var_oi >= lim and abs(var_px) < 1:
            tipo = "sobe"
            texto = (f"📈 **Open interest de {m} subiu {pct(var_oi, 1)} numa hora com o preço quase parado ({pct(var_px)}).** "
                     f"Muita gente a abrir posições ao mesmo tempo: combustível para um movimento forte para um dos lados.")
        elif var_oi <= -lim and abs(var_px) >= 1.5:
            tipo = "cai"
            texto = (f"🧨 **Open interest de {m} caiu {pct(var_oi, 1)} numa hora com o preço a mexer {pct(var_px)}.** "
                     f"Posições a ser fechadas à força: sinal de liquidações em cadeia.")
        if tipo:
            chave = f"oi-{tipo}-{m}"
            ult = alertas.get(chave)
            if not ult or agora - dt.datetime.fromisoformat(ult) > dt.timedelta(hours=2):
                enviar(texto)
                alertas[chave] = agora.isoformat()


def oi_24h(m, agora):
    pontos = ESTADO.get("oi_hist", {}).get(m, [])
    ts = agora.timestamp()
    antes = [p for p in pontos if 23 * 3600 <= ts - p[0] <= 26 * 3600]
    if not antes or not pontos:
        return None
    return (pontos[-1][1] / antes[0][1] - 1) * 100


# ------------------------------------------------------------------ fear & greed
def fear_greed():
    r = HTTP.get("https://api.alternative.me/fng/?limit=2", timeout=15)
    r.raise_for_status()
    d = r.json()["data"]
    trad = {"Extreme Fear": "medo extremo", "Fear": "medo", "Neutral": "neutro", "Greed": "ganância", "Extreme Greed": "ganância extrema"}
    hoje = f"{d[0]['value']} ({trad.get(d[0]['value_classification'], d[0]['value_classification'])})"
    ontem = f", ontem {d[1]['value']}" if len(d) > 1 else ""
    return f"Fear & Greed: {hoje}{ontem}"


# ------------------------------------------------------------------ relatório
def bloco_moeda(m, agora):
    s_ = simbolo_de(m)
    tk = ticker24(s_)
    preco = float(tk["lastPrice"])
    alto, baixo = float(tk["highPrice"]), float(tk["lowPrice"])
    pos24 = (preco - baixo) / (alto - baixo) * 100 if alto > baixo else 50
    dias = velas(s_, "1d", 8)
    var7 = (preco / dias[0]["o"] - 1) * 100
    alto7, baixo7 = max(v["h"] for v in dias[-7:]), min(v["l"] for v in dias[-7:])
    linhas = [f"**{m}** {fmt(preco)}  ({pct(float(tk['priceChangePercent']))} 24h, {pct(var7)} 7d)"]
    try:
        linhas.append(f"Tendência: 4h {tendencia(s_, '4h')}, diário {tendencia(s_, '1d')}")
    except Exception as e:
        log("tendencia", m, e)
    linhas.append(f"24h: mín {fmt(baixo)}, máx {fmt(alto)}, preço a {fmt(pos24, 0)}% do intervalo")
    linhas.append(f"7 dias: mín {fmt(baixo7)}, máx {fmt(alto7)}")
    try:
        f = hyperliquid().get(m.upper())
        if f:
            v24 = oi_24h(m, agora)
            linhas.append(f"Funding {fmt(f['funding8h'], 4)}% por 8h, open interest {fmt(f['oi_usd'] / 1e6, 0)} M$"
                          + (f" ({pct(v24, 1)} em 24h)" if v24 is not None else ""))
    except Exception as e:
        log("hyperliquid", e)
    nv = niveis_texto(m, preco)
    if nv:
        linhas.append(nv)
    return "\n".join(linhas)


def relatorio(nome, agora):
    local = agora.astimezone(TZ)
    partes = [f"📊 **Relatório da {nome}**, {local.strftime('%d/%m %H:%M')}"]
    if nome == "manhã" and CFG.get("fear_greed", True):
        try:
            partes[0] += "\n" + fear_greed()
        except Exception as e:
            log("fear_greed", e)
    for m in CFG.get("moedas", []):
        try:
            partes.append(bloco_moeda(m, agora))
        except Exception as e:
            log("relatorio", m, e)
            partes.append(f"**{m}**: sem dados agora.")
    partes.append("**Notícias de alto impacto hoje**\n" + eventos_hoje(agora))
    try:
        cands = scanner(agora, alertar=False)
        partes.append("**Ranges candidatos**\n" + ("\n".join("• " + c for c in cands) if cands else "Nenhum range claro agora."))
    except Exception as e:
        log("scanner_relatorio", e)
    rg = estado_ranges_texto()
    if rg:
        partes.append("**Os teus ranges**\n" + rg)
    if nome == "noite" and local.weekday() == 4:
        partes.append("🗓️ **Fim de semana à porta.** Menos liquidez, mais desvios falsos e movimentos sem continuação. "
                      "Se operares, sê mais exigente com os setups.")
    if nome == "noite" and local.weekday() == 6:
        partes.append("🗓️ **Próxima semana: notícias de alto impacto**\n" + eventos_proximos(agora))
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

    if MODO in ("teste", "relatorio"):
        try:
            atualizar_niveis_dia(agora)
        except Exception as e:
            log("niveis_dia", e)
        if MODO == "teste":
            enviar("✅ O bot está ligado a este canal. A seguir vem um relatório de teste.")
        relatorio("manhã" if agora.astimezone(TZ).hour < 15 else "noite", agora)

    hora_cheia = agora.minute < 15 or MODO in ("teste", "relatorio")
    for nome, f in [("niveis_dia", lambda: atualizar_niveis_dia(agora)),
                    ("aberturas", lambda: aberturas(ultima, agora)),
                    ("relatorios", lambda: relatorios(ultima, agora)),
                    ("calendario", lambda: avisos_calendario(agora)),
                    ("noticias", noticias),
                    ("movimentos", lambda: movimentos_e_funding(agora)),
                    ("open_interest", lambda: open_interest(agora)),
                    ("varrimentos", lambda: varrimentos(ultima)),
                    ("scanner", lambda: scanner(agora) if hora_cheia else None),
                    ("ranges", lambda: ranges_dmt(ultima))]:
        try:
            f()
        except Exception as e:
            log("falhou", nome, e)

    ESTADO["ultima_execucao"] = agora.isoformat()
    gravar_json("state.json", ESTADO)


if __name__ == "__main__":
    main()
