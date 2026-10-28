"""
Bot de mercado para Discord.

Relatórios bonitos de manhã e à noite, radar de moedas com setups de range, aberturas de
mercado com a liquidez da sessão anterior, calendário económico, notícias filtradas,
varrimentos de máximos e mínimos, open interest, funding, movimentos bruscos e alertas
nos ranges DMT. Tem controlo de spam: alertas agrupados, limite por hora e horas de silêncio.
Corre no GitHub Actions a cada 15 minutos.
"""
import datetime as dt
import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import feedparser
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
MODO = (os.environ.get("MODO") or "normal").strip().lower()
UTC = dt.timezone.utc
HTTP = requests.Session()
HTTP.headers["User-Agent"] = "bot-trading-dmt/2.0"


# ================================================================== utilidades
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


def barra(pos, n=10):
    cheio = max(0, min(n, round(pos / 100 * n)))
    return "▰" * cheio + "▱" * (n - cheio)


TENDENCIA_TXT = {"alta": "📈 alta", "baixa": "📉 baixa", "mista": "➖ mista"}


# ================================================================== Discord
CORES = {"azul": 0x3B82F6, "verde": 0x22C55E, "vermelho": 0xEF4444, "amarelo": 0xEAB308, "roxo": 0xA855F7,
         "ciano": 0x06B6D4, "cinza": 0x6B7280, "laranja": 0xF97316, "indigo": 0x6366F1}
FILA = []  # alertas desta execução: (prioridade, embed)


def embed(titulo, desc="", cor="azul", campos=None, url=None, rodape=None):
    e = {"title": titulo[:256], "color": CORES.get(cor, CORES["azul"])}
    if desc:
        e["description"] = desc[:4000]
    if url:
        e["url"] = url
    if campos:
        e["fields"] = [{"name": n[:256], "value": (v or "-")[:1024], "inline": inl} for n, v, inl in campos][:25]
    if rodape:
        e["footer"] = {"text": rodape[:2048]}
    return e


def alerta(e, prioridade="normal"):
    FILA.append((prioridade, e))


def publicar(payload):
    if not WEBHOOK:
        log("[sem webhook]", json.dumps(payload, ensure_ascii=False)[:3000])
        return
    for _ in range(4):
        r = HTTP.post(WEBHOOK, json=dict(payload, allowed_mentions={"parse": []}), timeout=20)
        if r.status_code == 429:
            try:
                time.sleep(float(r.json().get("retry_after", 2)) + 0.2)
            except Exception:
                time.sleep(2)
            continue
        if r.status_code >= 400:
            log("Erro Discord", r.status_code, r.text[:300])
        break
    time.sleep(0.7)


def tamanho(e):
    return len(json.dumps(e, ensure_ascii=False))


def enviar_embeds(lista):
    """Agrupa embeds em mensagens (até 10 por mensagem e 5.500 caracteres)."""
    grupo, total = [], 0
    for e in lista:
        t = tamanho(e)
        if grupo and (len(grupo) == 10 or total + t > 5500):
            publicar({"embeds": grupo})
            grupo, total = [], 0
        grupo.append(e)
        total += t
    if grupo:
        publicar({"embeds": grupo})


def em_silencio(agora):
    s = CFG.get("anti_spam", {}).get("silencio")
    if not s:
        return False
    hm = agora.astimezone(TZ).strftime("%H:%M")
    ini, fim = s["inicio"], s["fim"]
    return (ini <= hm < fim) if ini < fim else (hm >= ini or hm < fim)


def despachar(agora):
    sp = ESTADO.setdefault("spam", {})
    hora = agora.strftime("%Y-%m-%dT%H")
    if sp.get("hora") != hora:
        sp["hora"], sp["n"] = hora, 0
    maximo = CFG.get("anti_spam", {}).get("max_alertas_hora", 6)
    silencio = em_silencio(agora)
    saida = []
    for prio, e in FILA:
        if prio == "alta":
            saida.append(e)
        elif silencio or sp["n"] >= maximo:
            sp["silenciados"] = sp.get("silenciados", 0) + 1
        else:
            sp["n"] += 1
            saida.append(e)
    FILA.clear()
    enviar_embeds(saida)


# ================================================================== dados de mercado
def binance(caminho, params):
    r = HTTP.get("https://data-api.binance.vision/api/v3/" + caminho, params=params, timeout=20)
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
    global _HL
    if _HL is None:
        r = HTTP.post("https://api.hyperliquid.xyz/info", json={"type": "metaAndAssetCtxs"}, timeout=20)
        r.raise_for_status()
        meta, ctxs = r.json()
        _HL = {}
        for a, c in zip(meta["universe"], ctxs):
            try:
                _HL[a["name"]] = {"funding8h": float(c["funding"]) * 8 * 100,
                                  "oi_usd": float(c["openInterest"]) * float(c["markPx"]),
                                  "preco": float(c["markPx"])}
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
    fechos = [v["c"] for v in velas(simbolo, intervalo, 250)]
    if len(fechos) < 60:
        return "mista"
    preco, e50, e200 = fechos[-1], ema(fechos, 50), ema(fechos, min(200, len(fechos) - 1))
    if preco > e50 > e200:
        return "alta"
    if preco < e50 < e200:
        return "baixa"
    return "mista"


def simbolo_de(moeda):
    return moeda.upper() + CFG.get("cotacao", "USDT")


# ================================================================== tempo
def cruzou(hora, fuso, dias, ultima, agora):
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


# ================================================================== liquidez
def juntar_niveis(moeda, novos):
    lista = ESTADO.setdefault("liquidez", {}).setdefault("niveis", {}).setdefault(moeda, [])
    nomes = {n["nome"] for n in novos}
    lista[:] = [n for n in lista if n["nome"] not in nomes] + novos


def atualizar_niveis_dia(agora):
    liq = ESTADO.setdefault("liquidez", {})
    hoje = agora.date().isoformat()
    if liq.get("dia") == hoje:
        return
    liq["dia"], liq["niveis"] = hoje, {}
    for m in CFG.get("liquidez", {}).get("moedas", []):
        try:
            s_ = simbolo_de(m)
            ontem, semana = velas(s_, "1d", 2)[-2], velas(s_, "1w", 2)[-2]
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
                acima = n["tipo"] == "max"
                rompeu = v["c"] > n["preco"] if acima else v["c"] < n["preco"]
                tocou = v["h"] > n["preco"] if acima else v["l"] < n["preco"]
                if rompeu:
                    varr[k] = hoje
                    break
                if tocou:
                    alerta(embed(f"💧 {m} varreu o {n['nome']}",
                                 f"Passou {fmt(n['preco'])} e fechou de volta {'abaixo' if acima else 'acima'}, a {fmt(v['c'])}.\n"
                                 f"Os stops {'acima' if acima else 'abaixo'} foram apanhados: atenção a uma reação para "
                                 f"{'baixo' if acima else 'cima'}.", "ciano"))
                    varr[k] = hoje
                    break


def niveis_campo(moeda, preco):
    liq = ESTADO.get("liquidez", {})
    niveis = liq.get("niveis", {}).get(moeda, [])
    varr = liq.get("varridos", {})
    linhas = []
    for n in sorted(niveis, key=lambda n: -n["preco"]):
        dist = (n["preco"] / preco - 1) * 100
        feito = f"{moeda}|{n['nome']}|{n['preco']}" in varr
        seta = "▲" if n["preco"] >= preco else "▼"
        linhas.append(f"{seta} {n['nome']}: **{fmt(n['preco'])}** ({pct(dist)}){' ✓ tocado' if feito else ''}")
    return "\n".join(linhas)


# ================================================================== aberturas de mercado
def maximo_minimo(simbolo, inicio, fim):
    vs = [v for v in velas(simbolo, "15m", 60) if v["abre"] >= inicio.timestamp() and v["fecha"] <= fim.timestamp() + 1]
    return (max(v["h"] for v in vs), min(v["l"] for v in vs)) if vs else None


def aberturas(ultima, agora):
    for m in CFG.get("mercados", []):
        t = cruzou(m["hora"], m["fuso"], [0, 1, 2, 3, 4], ultima, agora)
        if not t:
            continue
        desc = m.get("nota", "")
        try:
            tk = ticker24(simbolo_de("BTC"))
            desc += f"\nBTC a **{fmt(float(tk['lastPrice']))}** ({pct(float(tk['priceChangePercent']))} em 24h)."
        except Exception as e:
            log("aberturas/ticker", e)
        campos = []
        ant = m.get("sessao_anterior")
        if ant:
            tz = ZoneInfo(ant["fuso"])
            d = t.astimezone(tz).date()
            hh, mm = map(int, ant["hora"].split(":"))
            inicio = dt.datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz)
            linhas = []
            for moeda in CFG.get("liquidez", {}).get("moedas", []):
                try:
                    r = maximo_minimo(simbolo_de(moeda), inicio, t)
                    if not r:
                        continue
                    alto, baixo = r
                    linhas.append(f"**{moeda}** ▲ {fmt(alto)}  ▼ {fmt(baixo)}")
                    juntar_niveis(moeda, [
                        {"nome": f"máximo da sessão {ant['rotulo']}", "preco": alto, "tipo": "max"},
                        {"nome": f"mínimo da sessão {ant['rotulo']}", "preco": baixo, "tipo": "min"},
                    ])
                except Exception as e:
                    log("sessao_anterior", moeda, e)
            if linhas:
                campos.append((f"Liquidez da sessão {ant['rotulo']}", "\n".join(linhas), False))
        alerta(embed(f"🔔 Abertura de {m['nome']}, {hora_local(t)} em Portugal", desc, "azul", campos), "alta")


# ================================================================== calendário económico
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
                        d = dt.datetime.fromisoformat(e["date"]).astimezone(UTC).replace(second=0, microsecond=0).isoformat()
                        k = e.get("title", "?") + d
                        if k not in vistos:
                            vistos.add(k)
                            eventos.append({"titulo": e.get("title", "?"), "moeda": e.get("country"), "data": d,
                                            "previsao": e.get("forecast") or "", "anterior": e.get("previous") or ""})
            except Exception as e:
                log("calendario", url, e)
        if algum:
            cal["eventos"], cal["obtido"] = eventos, agora.isoformat()
    return [dict(e, data=dt.datetime.fromisoformat(e["data"])) for e in cal.get("eventos", [])]


def avisos_calendario(agora):
    minutos = CFG["calendario"].get("aviso_min", 30)
    avisados = ESTADO.setdefault("eventos_avisados", [])
    for e in calendario(agora):
        chave = e["titulo"] + e["data"].isoformat()
        falta = (e["data"] - agora).total_seconds() / 60
        if 0 < falta <= minutos and chave not in avisados:
            campos = [(n, v, True) for n, v in (("Previsão", e["previsao"]), ("Anterior", e["anterior"])) if v]
            alerta(embed(f"⚠️ Daqui a {round(falta)} min: {e['moeda']} {e['titulo']}",
                         f"Às **{hora_local(e['data'])}**. Passo 1 da tua verificação: não abres trades em cima desta notícia.",
                         "amarelo", campos), "alta")
            avisados.append(chave)
    ESTADO["eventos_avisados"] = avisados[-200:]


def eventos_hoje(agora):
    hoje = agora.astimezone(TZ).date()
    evs = sorted((e for e in calendario(agora) if e["data"].astimezone(TZ).date() == hoje), key=lambda e: e["data"])
    if not evs:
        return "Sem notícias de alto impacto hoje."
    return "\n".join((f"~~{hora_local(e['data'])} {e['moeda']} {e['titulo']}~~ já saiu" if e["data"] <= agora
                      else f"🕒 **{hora_local(e['data'])}** {e['moeda']} {e['titulo']}") for e in evs)


def eventos_proximos(agora, dias=7):
    fim = agora + dt.timedelta(days=dias)
    evs = sorted((e for e in calendario(agora) if agora < e["data"] <= fim), key=lambda e: e["data"])
    if not evs:
        return "Sem notícias de alto impacto nos próximos dias (ou o calendário da próxima semana ainda não saiu)."
    dias_pt = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
    return "\n".join(f"**{dias_pt[e['data'].astimezone(TZ).weekday()]} {e['data'].astimezone(TZ).strftime('%d/%m %H:%M')}** "
                     f"{e['moeda']} {e['titulo']}" for e in evs)


# ================================================================== notícias
def noticias():
    n = CFG.get("noticias", {})
    vistos = set(ESTADO.get("noticias_vistas", []))
    primeira = not ESTADO.get("noticias_iniciadas")
    palavras = [p.lower() for p in n.get("palavras", [])] + [f" {m.lower()} " for m in CFG.get("moedas", [])]
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
                if any(p in " " + titulo.lower() + " " for p in palavras):
                    novas.append((feed["nome"], titulo, item.get("link", "")))
        except Exception as e:
            log("noticias", feed.get("nome"), e)
    ESTADO["noticias_vistas"] = list(vistos)[-600:]
    ESTADO["noticias_iniciadas"] = True
    if primeira:
        return
    for fonte, titulo, link in novas[: n.get("max_por_execucao", 3)]:
        alerta(embed(f"📰 {titulo}", f"Fonte: {fonte}", "cinza", url=link or None))


# ================================================================== movimentos, funding, open interest
def movimentos_e_funding(agora):
    alertas = ESTADO.setdefault("alertas_tempo", {})
    lim_mov = CFG.get("alertas", {}).get("movimento_1h_pct", 3)
    lim_fund = CFG.get("alertas", {}).get("funding_8h_pct", 0.05)
    for m in CFG.get("moedas", []):
        try:
            vs = velas(simbolo_de(m), "15m", 5)
            var = (vs[-1]["c"] / vs[0]["o"] - 1) * 100
            ult = alertas.get(f"mov-{m}")
            if abs(var) >= lim_mov and (not ult or agora - dt.datetime.fromisoformat(ult) > dt.timedelta(hours=1)):
                alerta(embed(f"{'🚀' if var > 0 else '🔻'} {m} {pct(var)} na última hora",
                             f"Está a **{fmt(vs[-1]['c'])}**.", "verde" if var > 0 else "vermelho"))
                alertas[f"mov-{m}"] = agora.isoformat()
        except Exception as e:
            log("movimento", m, e)
        try:
            f = hyperliquid().get(m.upper())
            ult = alertas.get(f"fund-{m}")
            if f and abs(f["funding8h"]) >= lim_fund and (not ult or agora - dt.datetime.fromisoformat(ult) > dt.timedelta(hours=8)):
                lado = ("Longs a pagar aos shorts: mercado carregado de compras." if f["funding8h"] > 0
                        else "Shorts a pagar aos longs: mercado carregado de vendas.")
                alerta(embed(f"💸 Funding extremo em {m}: {fmt(f['funding8h'], 4)}% por 8h",
                             lado + " Um lado apertado costuma acabar em limpeza de stops.", "laranja"))
                alertas[f"fund-{m}"] = agora.isoformat()
        except Exception as e:
            log("funding", m, e)


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
        pontos.append([round(ts), round(f["oi_usd"]), f["preco"]])
        hist[m] = pontos
        if not antes:
            continue
        var_oi = (f["oi_usd"] / antes[-1][1] - 1) * 100
        var_px = (f["preco"] / antes[-1][2] - 1) * 100
        e = None
        if var_oi >= lim and abs(var_px) < 1:
            tipo, e = "sobe", embed(f"📈 Open interest de {m} {pct(var_oi, 1)} numa hora",
                                    f"Com o preço quase parado ({pct(var_px)}). Muita gente a abrir posições ao mesmo tempo: "
                                    f"combustível para um movimento forte para um dos lados.", "roxo")
        elif var_oi <= -lim and abs(var_px) >= 1.5:
            tipo, e = "cai", embed(f"🧨 Open interest de {m} {pct(var_oi, 1)} numa hora",
                                   f"Com o preço a mexer {pct(var_px)}. Posições a ser fechadas à força: sinal de liquidações em cadeia.",
                                   "roxo")
        if e:
            ult = alertas.get(f"oi-{tipo}-{m}")
            if not ult or agora - dt.datetime.fromisoformat(ult) > dt.timedelta(hours=2):
                alerta(e)
                alertas[f"oi-{tipo}-{m}"] = agora.isoformat()


def oi_24h(m, agora):
    pontos = ESTADO.get("oi_hist", {}).get(m, [])
    antes = [p for p in pontos if 23 * 3600 <= agora.timestamp() - p[0] <= 26 * 3600]
    return (pontos[-1][1] / antes[0][1] - 1) * 100 if antes and pontos else None


def fear_greed():
    r = HTTP.get("https://api.alternative.me/fng/?limit=2", timeout=15)
    r.raise_for_status()
    d = r.json()["data"]
    trad = {"Extreme Fear": "medo extremo", "Fear": "medo", "Neutral": "neutro", "Greed": "ganância", "Extreme Greed": "ganância extrema"}
    v = int(d[0]["value"])
    icone = "😱" if v < 25 else "😟" if v < 45 else "😐" if v <= 55 else "🙂" if v <= 75 else "🤑"
    ontem = f", ontem {d[1]['value']}" if len(d) > 1 else ""
    return f"{icone} **Fear & Greed {v}**, {trad.get(d[0]['value_classification'], d[0]['value_classification'])}{ontem}\n{barra(v, 20)}"


# ================================================================== ranges (procura e radar)
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
        if abs(sum(v["c"] for v in jan[:terco]) / terco - sum(v["c"] for v in jan[-terco:]) / terco) > 0.35 * h:
            continue
        pontos = len(th) + len(tl)
        if melhor is None or pontos > melhor["toques"] or (pontos == melhor["toques"] and W > melhor["velas"]):
            melhor = {"topo": topo, "fundo": fundo, "velas": W, "toques": pontos, "cima": len(th), "baixo": len(tl),
                      "altura": h / fundo * 100}
    return melhor


def analisar_range(vs, tf, recentes=3):
    """Range definido sem as últimas velas, que servem para ver se houve desvio e regresso."""
    if len(vs) < 40:
        return None
    r = procurar_range(vs[:-recentes], tf)
    if not r:
        return None
    topo, fundo = r["topo"], r["fundo"]
    h = topo - fundo
    dl = h * CFG.get("radar", {}).get("dl_pct", 30) / 100
    rec = vs[-recentes:]
    preco = vs[-1]["c"]
    lo, hi = min(v["l"] for v in rec), max(v["h"] for v in rec)
    if lo < fundo - dl or hi > topo + dl or not (fundo <= preco <= topo):
        return None
    r.update(preco=preco, pos=(preco - fundo) / h * 100, dev_baixo=lo < fundo, dev_cima=hi > topo)
    return r


def pontuar(r, trend, rank, total):
    if r["dev_baixo"] and r["pos"] < 50:
        lado, sit, pts = "fundo", "desvio abaixo do fundo e de volta para dentro", 55
    elif r["dev_cima"] and r["pos"] > 50:
        lado, sit, pts = "topo", "desvio acima do topo e de volta para dentro", 55
    else:
        lado = "fundo" if r["pos"] <= 50 else "topo"
        dist = r["pos"] if lado == "fundo" else 100 - r["pos"]
        if dist > 25:
            return None
        sit, pts = f"perto do {lado}", 20 + (25 - dist) * 1.2
    if r["toques"] > 4:
        pts += min(15, (r["toques"] - 4) * 5)
    favor = (lado == "fundo" and trend == "alta") or (lado == "topo" and trend == "baixa")
    contra = (lado == "fundo" and trend == "baixa") or (lado == "topo" and trend == "alta")
    pts += 15 if favor else -10 if contra else 0
    pts += (1 - rank / max(total, 1)) * 10
    return {"pontos": max(0, min(100, round(pts))), "lado": lado, "situacao": sit,
            "direcao": "long" if lado == "fundo" else "short", "a_favor": favor, "contra": contra}


def universo():
    rd = CFG.get("radar", {})
    cot = CFG.get("cotacao", "USDT")
    excl = set(x.upper() for x in rd.get("excluir", []))
    out = []
    for t in binance("ticker/24hr", {}):
        s = t.get("symbol", "")
        if not s.endswith(cot) or s.endswith(("UP" + cot, "DOWN" + cot, "BULL" + cot, "BEAR" + cot)):
            continue
        base = s[: -len(cot)]
        try:
            qv = float(t["quoteVolume"])
        except (KeyError, ValueError):
            continue
        if base in excl or qv < rd.get("volume_min_usd", 20_000_000) or int(t.get("count", 1)) == 0:
            continue
        out.append({"moeda": base, "simbolo": s, "volume": qv})
    out.sort(key=lambda x: -x["volume"])
    return out[: rd.get("top_volume", 40)]


def radar(agora, alertar=True):
    rd = CFG.get("radar", {})
    uni = universo()
    tfs = rd.get("timeframes", {"1h": 120, "4h": 120})
    tarefas = [(i, c, tf, n) for i, c in enumerate(uni) for tf, n in tfs.items()]

    def trabalho(t):
        i, c, tf, n = t
        try:
            return i, c, tf, analisar_range(velas(c["simbolo"], tf, n), tf)
        except Exception as e:
            log("radar", c["simbolo"], tf, e)
            return i, c, tf, None

    with ThreadPoolExecutor(max_workers=8) as ex:
        resultados = list(ex.map(trabalho, tarefas))

    tendencias = {}
    candidatos = {}
    for i, c, tf, r in resultados:
        if not r:
            continue
        if c["moeda"] not in tendencias:
            try:
                tendencias[c["moeda"]] = tendencia(c["simbolo"], "1d")
            except Exception:
                tendencias[c["moeda"]] = "mista"
        p = pontuar(r, tendencias[c["moeda"]], i, len(uni))
        if not p:
            continue
        item = dict(r, **p, moeda=c["moeda"], tf=tf, tendencia=tendencias[c["moeda"]])
        if c["moeda"] not in candidatos or item["pontos"] > candidatos[c["moeda"]]["pontos"]:
            candidatos[c["moeda"]] = item
    lista = sorted(candidatos.values(), key=lambda x: -x["pontos"])
    ESTADO["radar"] = {"quando": agora.isoformat(), "analisadas": len(uni), "lista": lista[:12]}

    if alertar and rd.get("alertas", True):
        vistos = ESTADO.setdefault("radar_avisados", {})
        cd = dt.timedelta(hours=rd.get("cooldown_horas", 8))
        novos = []
        for x in lista:
            k = f"{x['moeda']}-{x['tf']}-{x['lado']}"
            ult = vistos.get(k)
            if x["pontos"] >= rd.get("pontuacao_min_alerta", 70) and (not ult or agora - dt.datetime.fromisoformat(ult) > cd):
                novos.append(x)
                vistos[k] = agora.isoformat()
        limite = (agora - dt.timedelta(days=3)).isoformat()
        ESTADO["radar_avisados"] = {k: v for k, v in vistos.items() if v >= limite}
        if novos:
            alerta(embed("🛰️ Radar: novos setups", "\n\n".join(linha_radar(x) for x in novos[:3]), "verde",
                         rodape="Setups para analisar, não sinais de entrada."))
    return lista


def linha_radar(x):
    seta = "🟢" if x["direcao"] == "long" else "🔴"
    trend = {"alta": "a favor" if x["a_favor"] else "contra" if x["contra"] else "neutra",
             "baixa": "a favor" if x["a_favor"] else "contra" if x["contra"] else "neutra"}.get(x["tendencia"], "neutra")
    return (f"{seta} **{x['moeda']}** {x['tf']}, **{x['pontos']} pts**, possível {x['direcao']}\n"
            f"{x['situacao'].capitalize()}. Range {fmt(x['fundo'])} a {fmt(x['topo'])} ({fmt(x['altura'], 1)}%), "
            f"{x['toques']} toques, tendência diária {x['tendencia']} ({trend}).")


# ================================================================== ranges DMT (niveis.json)
def chave_range(r):
    base = json.dumps({k: r.get(k) for k in ("par", "topo", "fundo", "dl", "lado")}, sort_keys=True)
    return r.get("id", "range") + "-" + hashlib.sha1(base.encode()).hexdigest()[:8]


def novo_estado(prev, low, close, preco, linha, dl, zona, meio):
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
    vivos = set()
    for r in [r for r in NIVEIS.get("ranges", []) if r.get("ativo")]:
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
                par = r["par"] + (f" ({r['nota']})" if r.get("nota") else "")
                lado_txt = "fundo" if sinal == 1 else "topo"
                if novo == "zona":
                    e = embed(f"🎯 {par}: a chegar ao {lado_txt} do range", f"Linha em **{fmt(abs(linha))}**. Prepara a verificação.", "azul")
                elif novo == "desvio":
                    e = embed(f"⚡ {par}: desvio para lá do {lado_txt}",
                              f"{fmt((linha - l) / h * 100, 1)}% da altura do range (DL a {fmt(dl, 0)}%). "
                              f"Espera o fecho de volta para dentro e o BOS.", "amarelo")
                elif novo == "reentrou":
                    e = embed(f"✅ {par}: de volta dentro do range", f"Depois do desvio no {lado_txt}. Agora procura o BOS no timeframe menor.", "verde")
                else:
                    e = embed(f"❌ {par}: passou a DL do {lado_txt}", "Pelo teu modelo, o range partiu. Atualiza ou desativa este range no niveis.json.", "vermelho")
                alerta(e, "alta")
        except Exception as e:
            log("range", r.get("id"), e)
    for k in list(estados):
        if not any(k.startswith(v) for v in vivos):
            del estados[k]


def ranges_dmt_texto():
    linhas = []
    for r in NIVEIS.get("ranges", []):
        if not r.get("ativo"):
            continue
        try:
            topo, fundo = float(r["topo"]), float(r["fundo"])
            preco = float(ticker24(r["par"])["lastPrice"])
            pos = (preco - fundo) / (topo - fundo) * 100
            sit = "acima do topo" if pos > 100 else "abaixo do fundo" if pos < 0 else f"{barra(pos)} {fmt(pos, 0)}%"
            linhas.append(f"**{r['par']}**{' (' + r['nota'] + ')' if r.get('nota') else ''}: {fmt(preco)}, {sit}")
        except Exception as e:
            log("ranges_dmt_texto", r.get("id"), e)
    return "\n".join(linhas)


# ================================================================== relatório
def embed_moeda(m, agora):
    s_ = simbolo_de(m)
    tk = ticker24(s_)
    preco = float(tk["lastPrice"])
    var24 = float(tk["priceChangePercent"])
    alto, baixo = float(tk["highPrice"]), float(tk["lowPrice"])
    pos24 = max(0, min(100, (preco - baixo) / (alto - baixo) * 100)) if alto > baixo else 50
    dias = velas(s_, "1d", 8)
    var7 = (preco / dias[0]["o"] - 1) * 100
    alto7, baixo7 = max(v["h"] for v in dias[-7:]), min(v["l"] for v in dias[-7:])
    pos7 = max(0, min(100, (preco - baixo7) / (alto7 - baixo7) * 100)) if alto7 > baixo7 else 50
    try:
        t4, t1 = tendencia(s_, "4h"), tendencia(s_, "1d")
        tend = f"4h {TENDENCIA_TXT[t4]}\ndiário {TENDENCIA_TXT[t1]}"
    except Exception as e:
        log("tendencia", m, e)
        tend = "-"
    campos = [
        ("24 horas", f"**{pct(var24)}**", True),
        ("7 dias", f"**{pct(var7)}**", True),
        ("Tendência", tend, True),
        ("Intervalo 24h", f"{fmt(baixo)} a {fmt(alto)}\n{barra(pos24)} {fmt(pos24, 0)}%", True),
        ("Intervalo 7 dias", f"{fmt(baixo7)} a {fmt(alto7)}\n{barra(pos7)} {fmt(pos7, 0)}%", True),
    ]
    try:
        f = hyperliquid().get(m.upper())
        if f:
            v24 = oi_24h(m, agora)
            campos.append(("Funding e OI", f"{fmt(f['funding8h'], 4)}% / 8h\n{fmt(f['oi_usd'] / 1e6, 0)} M$"
                           + (f" ({pct(v24, 1)})" if v24 is not None else ""), True))
    except Exception as e:
        log("hyperliquid", e)
    nv = niveis_campo(m, preco)
    if nv:
        campos.append(("Liquidez", nv, False))
    return embed(f"{'🟢' if var24 >= 0 else '🔴'} {m}  {fmt(preco)}", "", "verde" if var24 >= 0 else "vermelho", campos)


def relatorio(nome, agora):
    local = agora.astimezone(TZ)
    manha = nome == "manhã"
    desc = [f"**{local.strftime('%d/%m/%Y')}**, {local.strftime('%H:%M')}"]
    if manha and CFG.get("fear_greed", True):
        try:
            desc.append(fear_greed())
        except Exception as e:
            log("fear_greed", e)
    sp = ESTADO.get("spam", {})
    if manha and sp.get("silenciados"):
        desc.append(f"🔕 {sp['silenciados']} alertas menores ficaram em silêncio durante a noite ou por excesso.")
        sp["silenciados"] = 0
    campos = [("Notícias de alto impacto hoje", eventos_hoje(agora), False)]
    if nome == "noite" and local.weekday() == 4:
        campos.append(("🗓️ Fim de semana à porta", "Menos liquidez, mais desvios falsos e movimentos sem continuação. "
                       "Se operares, sê mais exigente com os setups.", False))
    if nome == "noite" and local.weekday() == 6:
        campos.append(("🗓️ Próxima semana", eventos_proximos(agora), False))
    embeds = [embed(f"{'☀️' if manha else '🌙'} Relatório da {nome}", "\n\n".join(desc), "indigo", campos)]

    for m in CFG.get("moedas", []):
        try:
            embeds.append(embed_moeda(m, agora))
        except Exception as e:
            log("relatorio", m, e)
            embeds.append(embed(f"{m}", "Sem dados agora.", "cinza"))

    rad = ESTADO.get("radar", {})
    if CFG.get("radar", {}).get("ativo", True) and rad.get("lista") is not None:
        top = [x for x in rad["lista"] if x["pontos"] >= CFG["radar"].get("pontuacao_min_relatorio", 40)]
        top = top[: CFG["radar"].get("max_no_relatorio", 5)]
        quando = hora_local(dt.datetime.fromisoformat(rad["quando"]))
        embeds.append(embed("🛰️ Radar: melhores setups agora",
                            "\n\n".join(linha_radar(x) for x in top) if top else "Nenhum setup de range interessante neste momento.",
                            "verde", rodape=f"{rad.get('analisadas', 0)} moedas analisadas às {quando}. Setups para analisar, não sinais de entrada."))
    rg = ranges_dmt_texto()
    if rg:
        embeds.append(embed("🎯 Os teus ranges", rg, "azul"))
    enviar_embeds(embeds)


def relatorios(ultima, agora):
    for nome, hora in CFG.get("relatorios", {}).items():
        if cruzou(hora, CFG.get("fuso", "Europe/Lisbon"), None, ultima, agora):
            relatorio(nome, agora)


# ================================================================== principal
def main():
    agora = dt.datetime.now(UTC)
    ultima = dt.datetime.fromisoformat(ESTADO["ultima_execucao"]) if ESTADO.get("ultima_execucao") else agora - dt.timedelta(minutes=15)
    ultima = max(ultima, agora - dt.timedelta(minutes=90))
    teste = MODO in ("teste", "relatorio")
    radar_ativo = CFG.get("radar", {}).get("ativo", True)
    hora_radar = agora.minute < 15 or teste

    def correr(nome, f):
        try:
            f()
        except Exception as e:
            log("falhou", nome, e)

    correr("niveis_dia", lambda: atualizar_niveis_dia(agora))
    if radar_ativo and hora_radar:
        correr("radar", lambda: radar(agora, alertar=not teste))
    if teste:
        if MODO == "teste":
            enviar_embeds([embed("✅ Bot ligado", "A seguir vem um relatório de teste.", "verde")])
        correr("relatorio_teste", lambda: relatorio("manhã" if agora.astimezone(TZ).hour < 15 else "noite", agora))
    for nome, f in [("aberturas", lambda: aberturas(ultima, agora)),
                    ("relatorios", lambda: relatorios(ultima, agora)),
                    ("calendario", lambda: avisos_calendario(agora)),
                    ("noticias", noticias),
                    ("movimentos", lambda: movimentos_e_funding(agora)),
                    ("open_interest", lambda: open_interest(agora)),
                    ("varrimentos", lambda: varrimentos(ultima)),
                    ("ranges", lambda: ranges_dmt(ultima))]:
        correr(nome, f)
    correr("despachar", lambda: despachar(agora))
    ESTADO["ultima_execucao"] = agora.isoformat()
    gravar_json("state.json", ESTADO)


if __name__ == "__main__":
    main()
