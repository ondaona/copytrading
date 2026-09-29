"""Gestor de copy trading en eToro, en papel (h416: copiar a los más regulares).

Cada día:   valora las copias con la rentabilidad pública de cada trader, aplica el stop
            y regenera el panel (docs/index.html).
Cada mes:   el día del cambio, rehace la selección (semanas y meses en verde de los últimos
            2 años) y rota las copias que ya no están entre el mejor 20 %.
Avisa por Telegram solo cuando pasa algo (apertura, cierre, stop, error) y con el resumen mensual.

Uso:
    python gestor.py                  ejecución normal (la que lanza GitHub Actions)
    python gestor.py --sin-telegram   igual, sin enviar mensajes
    python gestor.py --estado         imprime el estado y no toca nada
    python gestor.py --forzar-cambio  rehace la selección hoy aunque no toque
    python gestor.py --prueba         manda un mensaje de prueba a Telegram

Todo es simulado: no hay claves de eToro ni órdenes reales. La rentabilidad de cada trader es la
que publica eToro en dólares; el papel no simula el cambio euro/dólar.
"""
import datetime as dt
import html
import json
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request

AQUI = os.path.dirname(os.path.abspath(__file__))
F_CONFIG = os.path.join(AQUI, "config.json")
F_ESTADO = os.path.join(AQUI, "estado.json")
F_PANEL = os.path.join(AQUI, "docs", "index.html")
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
API = "https://www.etoro.com/sapi"
CRIPTO = 10  # TopTradedAssetClassId de las criptomonedas

try:
    from zoneinfo import ZoneInfo
    ZONA = ZoneInfo("Europe/Madrid")
except Exception:  # Windows sin tzdata
    ZONA = dt.timezone.utc


def ahora():
    return dt.datetime.now(ZONA)


def cargar(ruta, defecto=None):
    if not os.path.exists(ruta):
        return defecto
    with open(ruta, encoding="utf-8") as f:
        return json.load(f)


def guardar(ruta, datos):
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    tmp = ruta + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=1)
    os.replace(tmp, ruta)


# ---------------------------------------------------------------- red

def get(url, intentos=4):
    for i in range(intentos):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40) as r:
                return json.loads(r.read())
        except Exception as e:
            ultimo = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"no responde {url.split('?')[0]}: {ultimo}")


def pausa():
    time.sleep(1.5)  # eToro corta con Cloudflare si se le pide deprisa


def ranking_2a():
    """Popular Investors con sus rasgos de los últimos 2 años."""
    filas, pagina = [], 1
    while pagina <= 12:
        q = urllib.parse.urlencode({"istestaccount": "false", "period": "LastTwoYears", "pagesize": 500,
                                    "sort": "-profitableweekspct", "popularinvestor": "true", "page": pagina})
        j = get(f"{API}/rankings/rankings/?{q}")
        items = j.get("Items") or []
        filas += items
        if len(items) < 500:
            break
        pagina += 1
        pausa()
    return filas


def mes_en_curso(cid):
    """Rentabilidad del mes en curso (%) de un trader."""
    j = get(f"{API}/rankings/cid/{cid}/rankings/?Period=CurrMonth")
    return float((j.get("Data") or {}).get("Gain") or 0.0)


def meses_cerrados(cid):
    """{'AAAA-MM': rentabilidad %} de cada mes publicado."""
    j = get(f"{API}/userstats/gain/cid/{cid}/history")
    return {m["start"][:7]: float(m["gain"]) for m in j.get("monthly") or []}


def conjunto_mes():
    """Mediana del mes en curso entre los 500 Popular Investors más copiados (referencia del panel)."""
    q = urllib.parse.urlencode({"istestaccount": "false", "period": "CurrMonth", "pagesize": 500,
                                "sort": "-copiers", "popularinvestor": "true"})
    items = get(f"{API}/rankings/rankings/?{q}").get("Items") or []
    g = [float(x["Gain"]) for x in items if x.get("Gain") is not None]
    return statistics.median(g) if g else 0.0


# ---------------------------------------------------------------- selección

def seleccionar(cfg):
    """Devuelve (ordenados, candidatos): todo el universo filtrado por nota y los n primeros."""
    f = cfg["filtros"]
    uni = []
    for x in ranking_2a():
        if x.get("CopyBlock") or x.get("IsFund") or x.get("Blocked"):
            continue
        if f["excluir_cripto"] and x.get("TopTradedAssetClassId") == CRIPTO:
            continue
        if None in (x.get("ProfitableWeeksPct"), x.get("ProfitableMonthsPct"), x.get("Gain")):
            continue
        if (x.get("WeeksSinceRegistration") or 0) < f["semanas_registrado_min"]:
            continue
        if (x.get("ActiveWeeks") or 0) < f["semanas_activas_2a_min"]:
            continue
        if (x.get("RiskScore") or 10) > f["riesgo_max"]:
            continue
        if x["Gain"] < f["ganancia_2a_min_pct"]:
            continue
        uni.append({"cid": x["CustomerId"], "usuario": x["UserName"], "pais": x.get("Country") or "",
                    "semanas_verde": x["ProfitableWeeksPct"], "meses_verde": x["ProfitableMonthsPct"],
                    "ganancia_2a": x["Gain"], "riesgo": x.get("RiskScore"), "caida_max": x.get("PeakToValley"),
                    "copiadores": x.get("Copiers"),
                    "nota": round(x["ProfitableWeeksPct"] / 100 + x["ProfitableMonthsPct"] / 100, 4)})
    uni.sort(key=lambda r: (-r["nota"], -r["semanas_verde"], r["usuario"]))
    for i, r in enumerate(uni):
        r["puesto"] = i + 1
    return uni


# ---------------------------------------------------------------- cartera en papel

def nueva_copia(c, importe, cfg, mes, mtd, fecha):
    neto = importe * (1 - cfg["coste_por_lado"])
    return {"cid": c["cid"], "usuario": c["usuario"], "invertido": round(neto, 2), "valor": round(neto, 2),
            "entrada": fecha, "ref_mes": mes, "ref_mtd": mtd, "ref_valor": round(neto, 2),
            "semanas_verde": c["semanas_verde"], "meses_verde": c["meses_verde"], "ganancia_2a": c["ganancia_2a"],
            "riesgo": c["riesgo"]}


def valorar(copia, mes, mtd, cerrados):
    """Valor de hoy encadenando la rentabilidad publicada desde la referencia guardada."""
    if mes == copia["ref_mes"]:
        v = copia["ref_valor"] * (1 + mtd / 100) / (1 + copia["ref_mtd"] / 100)
    else:
        g_ref = cerrados.get(copia["ref_mes"], copia["ref_mtd"])
        v = copia["ref_valor"] * (1 + g_ref / 100) / (1 + copia["ref_mtd"] / 100)
        m = siguiente_mes(copia["ref_mes"])
        while m < mes:
            v *= 1 + cerrados.get(m, 0.0) / 100
            m = siguiente_mes(m)
        v *= 1 + mtd / 100
        copia["ref_mes"], copia["ref_mtd"], copia["ref_valor"] = mes, mtd, round(v, 4)
    copia["valor"] = round(v, 2)


def vetado(est, cid, mes):
    hasta = est.get("vetados", {}).get(str(cid))
    return hasta is not None and mes < hasta


def sumar_meses(m, n):
    for _ in range(n):
        m = siguiente_mes(m)
    return m


def siguiente_mes(m):
    a, b = int(m[:4]), int(m[5:7])
    return f"{a + (b == 12)}-{b % 12 + 1:02d}"


def estado_vacio(cfg):
    return {"modo": cfg["modo"], "inicio": None, "capital_inicial": cfg["capital_inicial_eur"],
            "efectivo": float(cfg["capital_inicial_eur"]), "copias": [], "ultimo_cambio": None,
            "seleccion": [], "historia": [], "registro": [], "costes": 0.0,
            "referencia": {"ref_mes": None, "ref_mtd": 0.0, "ref_valor": 100.0, "valor": 100.0},
            "ultima_ejecucion": None, "ultimo_error": None}


def apuntar(est, texto, eventos):
    est["registro"].insert(0, {"fecha": ahora().strftime("%Y-%m-%d %H:%M"), "texto": texto})
    del est["registro"][300:]
    eventos.append(texto)


def eur(x):
    return f"{x:,.0f} €".replace(",", ".").replace("-", "−")


def pct(x, dec=1):
    return f"{x * 100:+.{dec}f} %".replace(".", ",").replace("-", "−")


def abrir(est, cfg, cand, importe, mes, eventos, motivo):
    pausa()
    mtd = mes_en_curso(cand["cid"])
    c = nueva_copia(cand, importe, cfg, mes, mtd, ahora().strftime("%Y-%m-%d"))
    est["efectivo"] = round(est["efectivo"] - importe, 2)
    est["costes"] = round(est["costes"] + importe - c["invertido"], 2)
    est["copias"].append(c)
    apuntar(est, f"Abierta la copia de {cand['usuario']} con {eur(importe)} ({motivo})", eventos)


def cerrar(est, cfg, copia, eventos, motivo):
    neto = copia["valor"] * (1 - cfg["coste_por_lado"])
    est["efectivo"] = round(est["efectivo"] + neto, 2)
    est["costes"] = round(est["costes"] + copia["valor"] - neto, 2)
    est["copias"] = [c for c in est["copias"] if c["cid"] != copia["cid"]]
    r = copia["valor"] / copia["invertido"] - 1
    apuntar(est, f"Cerrada la copia de {copia['usuario']} en {eur(copia['valor'])} ({pct(r)}; {motivo})", eventos)


def rebalancear(est, cfg, eventos):
    uni = seleccionar(cfg)
    if len(uni) < cfg["numero_de_traders"]:
        raise RuntimeError(f"solo {len(uni)} traders pasan los filtros")
    n = cfg["numero_de_traders"]
    corte = max(n, int(len(uni) * cfg["mantener_si_sigue_en_el_mejor"]))
    puesto = {r["cid"]: r for r in uni}
    mes = ahora().strftime("%Y-%m")
    for c in list(est["copias"]):
        r = puesto.get(c["cid"])
        if r is None:
            cerrar(est, cfg, c, eventos, "ya no pasa los filtros")
        elif r["puesto"] > corte:
            cerrar(est, cfg, c, eventos, f"baja al puesto {r['puesto']} de {len(uni)}")
        else:
            c.update({k: r[k] for k in ("semanas_verde", "meses_verde", "ganancia_2a", "riesgo")})
    tengo = {c["cid"] for c in est["copias"]}
    entran = [r for r in uni if r["cid"] not in tengo and not vetado(est, r["cid"], mes)][: n - len(est["copias"])]
    if entran:
        total = est["efectivo"] + sum(c["valor"] for c in est["copias"])
        por_copia = min(total / n, est["efectivo"] / len(entran))
        for r in entran:
            if por_copia < cfg["minimo_por_copia_eur"]:
                apuntar(est, f"No se abre {r['usuario']}: {eur(por_copia)} no llega al mínimo de eToro", eventos)
                continue
            abrir(est, cfg, r, por_copia, mes, eventos, f"puesto {r['puesto']} de {len(uni)}")
    est["seleccion"] = uni[:25]
    est["universo"] = len(uni)
    est["ultimo_cambio"] = mes


def diario(est, cfg, eventos):
    hoy = ahora()
    mes = hoy.strftime("%Y-%m")
    for c in list(est["copias"]):
        pausa()
        mtd = mes_en_curso(c["cid"])
        cerrados = {}
        if mes != c["ref_mes"]:
            pausa()
            cerrados = meses_cerrados(c["cid"])
        valorar(c, mes, mtd, cerrados)
        if c["valor"] <= c["invertido"] * (1 - cfg["stop_por_trader"]):
            cerrar(est, cfg, c, eventos, f"stop del {cfg['stop_por_trader'] * 100:.0f} %")
            est.setdefault("vetados", {})[str(c["cid"])] = sumar_meses(mes, 3)
            reponer(est, cfg, mes, eventos)
    # referencia: mediana de los 500 Popular Investors más copiados, encadenada como una copia más
    pausa()
    ref = est["referencia"]
    mtd_ref = conjunto_mes()
    if ref["ref_mes"] is None:
        ref.update({"ref_mes": mes, "ref_mtd": mtd_ref, "ref_valor": 100.0})
    elif mes != ref["ref_mes"]:
        ref["ref_valor"] = ref["ref_valor"] * (1 + ref.get("ultimo_mtd", ref["ref_mtd"]) / 100) / (1 + ref["ref_mtd"] / 100)
        ref["ref_mes"], ref["ref_mtd"] = mes, 0.0
    ref["valor"] = round(ref["ref_valor"] * (1 + mtd_ref / 100) / (1 + ref["ref_mtd"] / 100), 3)
    ref["ultimo_mtd"] = mtd_ref


def reponer(est, cfg, mes, eventos):
    """Tras un stop, el dinero liberado va al siguiente de la última selección que no se copie ya."""
    tengo = {c["cid"] for c in est["copias"]}
    for r in est.get("seleccion", []):
        if r["cid"] in tengo or vetado(est, r["cid"], mes):
            continue
        importe = est["efectivo"]
        if importe < cfg["minimo_por_copia_eur"]:
            return
        abrir(est, cfg, r, importe, mes, eventos, "sustituye a una copia cerrada por el stop")
        return


def total(est):
    return est["efectivo"] + sum(c["valor"] for c in est["copias"])


# ---------------------------------------------------------------- Telegram

def credenciales():
    env = {k: os.environ.get(k) for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
    local = os.path.join(AQUI, "..", "..", ".env")
    if not all(env.values()) and os.path.exists(local):
        for linea in open(local, encoding="utf-8"):
            if "=" in linea and not linea.startswith("#"):
                k, v = linea.strip().split("=", 1)
                if k in env and not env[k]:
                    env[k] = v.strip().strip('"')
    return env


def telegram(texto):
    env = credenciales()
    if not all(env.values()):
        print("(sin credenciales de Telegram)")
        return False
    datos = urllib.parse.urlencode({"chat_id": env["TELEGRAM_CHAT_ID"], "text": texto, "parse_mode": "HTML",
                                    "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{env['TELEGRAM_BOT_TOKEN']}/sendMessage",
                               data=datos, timeout=25)
        return True
    except Exception as e:
        print("error de Telegram:", e)
        return False


def mensaje(est, cfg, eventos, mensual):
    v = total(est)
    r = v / est["capital_inicial"] - 1
    cab = "📊 <b>Copy trading eToro · resumen del mes</b>" if mensual else "📋 <b>Copy trading eToro</b>"
    lineas = [cab, f"<i>En papel: dinero simulado, sin órdenes reales.</i>", "",
              f"Cartera: <b>{eur(v)}</b> ({pct(r)} sobre {eur(est['capital_inicial'])})"]
    if eventos:
        lineas += ["", *[f"• {html.escape(e)}" for e in eventos]]
    elif mensual:
        lineas += ["", f"Sin rotaciones: los {len(est['copias'])} traders siguen entre el mejor "
                       f"{round(cfg['mantener_si_sigue_en_el_mejor'] * 100)} % y pasan los filtros."]
    if mensual:
        lineas += ["", "Copias:"] + [f"• {html.escape(c['usuario'])}: {eur(c['valor'])} ({pct(c['valor'] / c['invertido'] - 1)})"
                                     for c in sorted(est["copias"], key=lambda c: -c["valor"])]
    lineas += ["", f"Panel: {cfg['panel_url']}"]
    return "\n".join(lineas)


# ---------------------------------------------------------------- panel

def panel(est, cfg):
    v = total(est)
    ini = est["capital_inicial"]
    ref = est["referencia"]["valor"] / 100 - 1
    copias = sorted(est["copias"], key=lambda c: c["valor"] / c["invertido"])
    peor = copias[0] if copias else None
    stop = cfg["stop_por_trader"]
    filas = []
    for c in sorted(est["copias"], key=lambda c: -c["valor"] / c["invertido"]):
        r = c["valor"] / c["invertido"] - 1
        usado = max(0.0, -r) / stop
        color = "var(--rojo)" if usado > .5 else "var(--ambar)" if usado > .2 else "var(--verde)"
        filas.append(
            f'<div class="fila"><span class="nom"><a href="https://www.etoro.com/people/{html.escape(c["usuario"].lower())}">'
            f'{html.escape(c["usuario"])}</a><small>desde {c["entrada"]} · {c["semanas_verde"]:.0f} % semanas en verde · riesgo {c["riesgo"]}</small></span>'
            f'<span class="num">{eur(c["valor"])}</span><span class="num {"pos" if r >= 0 else "neg"}">{pct(r)}</span>'
            f'<span class="barra"><i style="width:{max(3, min(100, usado * 100)):.0f}%;background:{color}"></i></span></div>')
    hist = est["historia"][-400:]
    svg = ""
    if len(hist) >= 2:
        xs = [h["valor"] for h in hist]
        lo, hi = min(xs + [ini]), max(xs + [ini])
        hi = hi if hi > lo else lo + 1
        W, H = 640, 140
        pts = " ".join(f"{i / (len(xs) - 1) * W:.1f},{H - (x - lo) / (hi - lo) * (H - 10) - 5:.1f}" for i, x in enumerate(xs))
        y0 = H - (ini - lo) / (hi - lo) * (H - 10) - 5
        svg = (f'<svg viewBox="0 0 {W} {H}" preserveAspectRatio="none" role="img" aria-label="Evolución del valor de la cartera">'
               f'<line x1="0" x2="{W}" y1="{y0:.1f}" y2="{y0:.1f}" class="base"/><polyline points="{pts}" class="linea"/></svg>'
               f'<p class="pie">{hist[0]["fecha"]} → {hist[-1]["fecha"]} · línea gris: capital inicial</p>')
    else:
        svg = '<p class="pie">La gráfica aparece a partir del segundo día.</p>'
    registro = "".join(f'<li><time>{html.escape(e["fecha"])}</time> {html.escape(e["texto"])}</li>' for e in est["registro"][:40])
    sel = "".join(
        f'<tr><td>{r["puesto"]}</td><td>{html.escape(r["usuario"])}</td><td>{r["semanas_verde"]:.0f} %</td><td>{r["meses_verde"]:.0f} %</td>'
        f'<td>{r["ganancia_2a"]:+.0f} %</td><td>{r["riesgo"]}</td><td>{"sí" if r["cid"] in {c["cid"] for c in est["copias"]} else ""}</td></tr>'
        for r in est.get("seleccion", [])[:15])
    err = (f'<div class="aviso neg"><b>Error en la última ejecución:</b> {html.escape(est["ultimo_error"])}</div>'
           if est.get("ultimo_error") else "")
    modo = "Papel (dinero simulado)" if est["modo"] == "papel" else est["modo"]
    prox = siguiente_mes(est["ultimo_cambio"]) + f"-{cfg['dia_del_cambio']:02d}" if est.get("ultimo_cambio") else "pendiente"
    datos = dict(valor=eur(v), dif=f"{v - ini:+,.0f} €".replace(",", ".").replace("-", "−"), pct=f"{pct(v / ini - 1)}",
                 ref=f"{pct(ref)}", vs=f"{(v / ini - 1 - ref) * 100:+.1f} pt".replace(".", ",").replace("-", "−"),
                 peor=f"{pct(peor['valor'] / peor['invertido'] - 1)}" if peor else "—",
                 peor_n=html.escape(peor["usuario"]) if peor else "sin copias",
                 costes=eur(est["costes"]), efectivo=eur(est["efectivo"]), stop=f"{stop:.0%}",
                 n=len(est["copias"]), ultima=html.escape(est.get("ultima_ejecucion") or "—"), prox=prox,
                 modo=modo, inicio=est.get("inicio") or "—", universo=est.get("universo", "—"))
    pagina = PLANTILLA
    for k, val in datos.items():
        pagina = pagina.replace("{{" + k + "}}", str(val))
    pagina = (pagina.replace("{{filas}}", "".join(filas) or '<p class="pie">Sin copias abiertas.</p>')
              .replace("{{grafica}}", svg).replace("{{registro}}", registro).replace("{{seleccion}}", sel).replace("{{error}}", err))
    os.makedirs(os.path.dirname(F_PANEL), exist_ok=True)
    with open(F_PANEL, "w", encoding="utf-8") as f:
        f.write(pagina)


PLANTILLA = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Copy trading eToro</title>
<meta name="robots" content="noindex">
<style>
:root{--fondo:#f7f6f3;--tarjeta:#fff;--suave:#efede8;--texto:#1f1e1c;--sec:#6b6a65;--linea:#e2e0da;--verde:#1d9e75;--ambar:#ef9f27;--rojo:#e24b4a;--pos:#0f6e56;--neg:#a32d2d;--acento:#185fa5}
@media (prefers-color-scheme:dark){:root{--fondo:#1b1b1a;--tarjeta:#242423;--suave:#2c2c2a;--texto:#ecebe7;--sec:#a8a7a1;--linea:#3a3a37;--pos:#5dcaa5;--neg:#f09595;--acento:#85b7eb}}
*{box-sizing:border-box}body{margin:0;background:var(--fondo);color:var(--texto);font:15px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:860px;margin:0 auto;padding:24px 16px 48px}h1{font-size:22px;font-weight:600;margin:0}h2{font-size:16px;font-weight:600;margin:28px 0 8px}
a{color:var(--acento);text-decoration:none}.cab{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:16px}
.pill{font-size:12px;padding:3px 10px;border-radius:8px;background:#faeeda;color:#633806;margin-right:6px}.pill.ok{background:#e1f5ee;color:#085041}
@media (prefers-color-scheme:dark){.pill{background:#633806;color:#fac775}.pill.ok{background:#085041;color:#9fe1cb}}
.meta{font-size:12px;color:var(--sec)}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.card{background:var(--tarjeta);border:1px solid var(--linea);border-radius:12px;padding:12px 14px}.card p{margin:0}.lab{font-size:13px;color:var(--sec)}
.big{font-size:24px;font-weight:600}.pos{color:var(--pos)}.neg{color:var(--neg)}
.caja{background:var(--tarjeta);border:1px solid var(--linea);border-radius:12px;padding:8px 14px}
.fila{display:grid;grid-template-columns:minmax(0,1.6fr) 80px 70px minmax(0,1fr);gap:10px;align-items:center;padding:9px 0;border-bottom:1px solid var(--linea)}
.fila:last-child{border-bottom:0}.nom small{display:block;font-size:12px;color:var(--sec)}.num{text-align:right;font-variant-numeric:tabular-nums}
.barra{height:7px;border-radius:4px;background:var(--suave);position:relative;overflow:hidden}.barra i{position:absolute;left:0;top:0;bottom:0;border-radius:4px}
.cabfila{font-size:12px;color:var(--sec);border-bottom:1px solid var(--linea)}
svg{width:100%;height:140px;display:block}.linea{fill:none;stroke:var(--acento);stroke-width:2;vector-effect:non-scaling-stroke}.base{stroke:var(--sec);stroke-dasharray:4 4;vector-effect:non-scaling-stroke}
.pie{font-size:12px;color:var(--sec);margin:6px 0 0}ul{list-style:none;padding:0;margin:0}li{padding:6px 0;border-bottom:1px solid var(--linea);font-size:14px}li:last-child{border-bottom:0}
time{color:var(--sec);font-size:12px;margin-right:6px}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:6px 4px;border-bottom:1px solid var(--linea)}th{color:var(--sec);font-weight:500}
.aviso{border:1px solid var(--rojo);border-radius:12px;padding:10px 14px;margin-bottom:12px}
@media (max-width:560px){.fila{grid-template-columns:minmax(0,1fr) 70px 60px}.barra{grid-column:1/-1}}
</style></head><body><main>
<div class="cab"><h1>Copy trading eToro</h1><div><span class="pill">{{modo}}</span><span class="pill ok">Automático</span></div></div>
<p class="meta">Inicio {{inicio}} · última ejecución {{ultima}} · próximo cambio {{prox}} · stop por trader {{stop}}</p>
{{error}}
<div class="cards">
<div class="card"><p class="lab">Valor de la cartera</p><p class="big">{{valor}}</p><p class="lab">{{dif}} ({{pct}})</p></div>
<div class="card"><p class="lab">Frente al conjunto</p><p class="big">{{vs}}</p><p class="lab">500 más copiados: {{ref}}</p></div>
<div class="card"><p class="lab">Copia más cerca del stop</p><p class="big">{{peor}}</p><p class="lab">{{peor_n}}</p></div>
<div class="card"><p class="lab">Costes y efectivo</p><p class="big">{{costes}}</p><p class="lab">{{efectivo}} sin invertir</p></div>
</div>
<h2>Evolución</h2><div class="caja">{{grafica}}</div>
<h2>Copias abiertas ({{n}})</h2>
<div class="caja"><div class="fila cabfila"><span>Trader</span><span class="num">Valor</span><span class="num">Resultado</span><span>Distancia al stop</span></div>{{filas}}</div>
<h2>Registro</h2><div class="caja"><ul>{{registro}}</ul></div>
<h2>Selección del mes</h2>
<div class="caja"><table><tr><th>Puesto</th><th>Trader</th><th>Semanas en verde</th><th>Meses en verde</th><th>2 años</th><th>Riesgo</th><th>Copiado</th></tr>{{seleccion}}</table>
<p class="pie">{{universo}} Popular Investors pasan los filtros (≥140 semanas, sin cripto, riesgo ≤6, +5 % en 2 años). Orden: semanas + meses en verde de los últimos 2 años.</p></div>
<p class="pie">Simulación en papel con la rentabilidad pública de cada trader en eToro (en dólares; no incluye el cambio euro/dólar). Coste supuesto: 0,25 % por apertura y por cierre.</p>
</main></body></html>"""


# ---------------------------------------------------------------- principal

def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    args = set(sys.argv[1:])
    cfg = cargar(F_CONFIG)
    est = cargar(F_ESTADO) or estado_vacio(cfg)
    if "--prueba" in args:
        enviado = telegram("🔧 Prueba del gestor de copy trading (papel).")
        print("enviado" if enviado else "no enviado")
        return 0 if enviado else 1
    if "--estado" in args:
        print(json.dumps({k: est[k] for k in ("modo", "inicio", "efectivo", "ultimo_cambio", "ultima_ejecucion")}, ensure_ascii=False))
        for c in est["copias"]:
            print(f"  {c['usuario']:20s} {c['valor']:9.2f} {c['valor'] / c['invertido'] - 1:+.2%}")
        print("total", round(total(est), 2))
        return 0
    eventos, mensual = [], False
    hoy = ahora()
    try:
        if est["inicio"] is None:
            est["inicio"] = hoy.strftime("%Y-%m-%d")
        diario(est, cfg, eventos)
        toca = est["ultimo_cambio"] != hoy.strftime("%Y-%m") and hoy.day >= cfg["dia_del_cambio"]
        if toca or not est["copias"] or "--forzar-cambio" in args:
            rebalancear(est, cfg, eventos)
            mensual = True
        est["ultimo_error"] = None
        codigo = 0
    except Exception as e:
        est["ultimo_error"] = f"{hoy:%Y-%m-%d %H:%M}: {e}"
        eventos.append(f"⚠️ Error: {e}. No se ha tocado ninguna copia; se reintenta en la próxima ejecución.")
        codigo = 1
    est["ultima_ejecucion"] = hoy.strftime("%Y-%m-%d %H:%M")
    fecha = hoy.strftime("%Y-%m-%d")
    est["historia"] = [h for h in est["historia"] if h["fecha"] != fecha] + [{"fecha": fecha, "valor": round(total(est), 2),
                                                                              "referencia": est["referencia"]["valor"]}]
    guardar(F_ESTADO, est)
    panel(est, cfg)
    print(f"total {total(est):.2f} €, {len(est['copias'])} copias; eventos: {len(eventos)}")
    for e in eventos:
        print(" -", e)
    if (eventos or mensual) and "--sin-telegram" not in args:
        telegram(mensaje(est, cfg, eventos, mensual))
    return codigo


if __name__ == "__main__":
    sys.exit(main())
