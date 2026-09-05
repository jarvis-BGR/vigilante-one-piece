#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Vigilante One Piece — catalogo unico + datos en vivo
Modos:
  --modo vigilar  (por defecto): revisa SOLO los productos vigilados
                  (productos.json) en todas sus tiendas y AVISA por Telegram
                  cuando hay un cambio relevante. Refresca sus datos en vivo.
  --modo catalogo: revisa TODO el catalogo (catalogo.json) en todas sus
                  tiendas y solo refresca los datos en vivo (SIN avisos).
No compra ni reserva nada: solo informa.
"""
import json, os, re, html, sys, datetime
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
CATALOGO = os.path.join(BASE, "catalogo.json")
PRODUCTOS = os.path.join(BASE, "productos.json")
ESTADO = os.path.join(BASE, "estado.json")
DATOS = os.path.join(BASE, "datos.json")

TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
EMOJI = {"DISPONIBLE": "🟢", "PREVENTA": "🟡", "AGOTADO": "🔴", "DESCONOCIDO": "⚪"}
FECHA_RE = re.compile(r'(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})')
PREVENTA_KW = ("preventa", "pre-venta", "preorder", "pre-order", "reserva",
               "próximamente", "proximamente")


def cargar_json(ruta, por_defecto):
    try:
        with open(ruta, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return por_defecto


def guardar_json(ruta, datos):
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)


def http_get(url):
    return requests.get(url, headers={"User-Agent": UA,
                        "Accept-Language": "es-ES,es;q=0.9"}, timeout=30)


# ---------- lectura de fichas ----------
def leer_metas(contenido):
    metas = {}
    for m in re.finditer(r"<meta\b[^>]*>", contenido, re.I):
        tag = m.group(0)
        k = re.search(r'(?:property|name)\s*=\s*(["\'])(.*?)\1', tag, re.I)
        v = re.search(r'content\s*=\s*(["\'])(.*?)\1', tag, re.I)
        if k and v:
            metas[k.group(2).lower()] = html.unescape(v.group(2))
    return metas


def buscar_fecha(texto):
    for kw in ("preventa", "reserva", "disponible el", "fecha de salida",
               "fecha de lanzamiento", "lanzamiento", "salida"):
        idx = texto.lower().find(kw)
        if idx != -1:
            m = FECHA_RE.search(texto, idx, idx + 160)
            if m:
                return m.group(1)
    return ""


def precio_num(valor):
    if valor is None:
        return ""
    m = re.search(r'(\d+[.,]?\d*)', str(valor))
    return m.group(1).replace(",", ".") if m else ""


def json_ld_producto(contenido):
    for m in re.finditer(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                         contenido, re.I | re.S):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        cand = data if isinstance(data, list) else \
            (data.get("@graph", [data]) if isinstance(data, dict) else [])
        for obj in cand:
            if not isinstance(obj, dict):
                continue
            t = obj.get("@type", "")
            if (t == "Product" or (isinstance(t, list) and "Product" in t)) and obj.get("offers"):
                offers = obj["offers"]
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                return (str(offers.get("availability", "")), precio_num(offers.get("price")),
                        offers.get("priceCurrency", ""), obj.get("name", ""))
    return ("", "", "", "")


def estado_schema(av):
    a = (av or "").lower()
    if "preorder" in a or "backorder" in a: return "PREVENTA"
    if "outofstock" in a or "soldout" in a or "discontinued" in a: return "AGOTADO"
    if "instock" in a or "onlineonly" in a or "limitedavailability" in a: return "DISPONIBLE"
    return ""


def estado_meta(av):
    a = (av or "").strip().lower()
    return {"in stock": "DISPONIBLE", "instock": "DISPONIBLE",
            "available for order": "PREVENTA", "preorder": "PREVENTA", "backorder": "PREVENTA",
            "out of stock": "AGOTADO", "discontinued": "AGOTADO"}.get(a, "")


def afina_preventa(estado, texto, titulo=""):
    if estado == "DISPONIBLE":
        t = (texto + " " + (titulo or "")).lower()
        if any(k in t for k in PREVENTA_KW):
            return "PREVENTA"
    return estado


def leer_shopify(url):
    base = url.split("?")[0].rstrip("/")
    try:
        r = http_get(base + ".json")
        if not r.ok: return None
        prod = r.json().get("product")
        if not prod: return None
    except Exception:
        return None
    variants = prod.get("variants", []) or []
    disponible = any(v.get("available") for v in variants)
    precio = precio_num(variants[0].get("price")) if variants else ""
    nombre = prod.get("title", "")
    estado = "DISPONIBLE" if disponible else "AGOTADO"
    fecha = ""
    try:
        texto = re.sub(r"<[^>]+>", " ", http_get(base).text)
        estado = afina_preventa(estado, texto, nombre)
        fecha = buscar_fecha(texto)
    except Exception:
        pass
    return {"nombre": nombre, "estado": estado, "precio": precio, "moneda": "EUR",
            "fecha": fecha, "raw": "shopify:" + str(disponible)}


def leer_generico(url):
    r = http_get(url); r.raise_for_status()
    contenido = r.text
    texto = re.sub(r"<[^>]+>", " ", contenido)
    metas = leer_metas(contenido)
    nombre = metas.get("og:title", "")
    precio = precio_num(metas.get("product:price:amount"))
    moneda = metas.get("product:price:currency", "EUR")
    estado = estado_meta(metas.get("product:availability", ""))
    raw = "meta:" + metas.get("product:availability", "")
    if not estado:
        av, p, cur, nm = json_ld_producto(contenido)
        e = estado_schema(av)
        if e:
            estado, raw = e, "jsonld:" + av
            precio = precio or p
            moneda = cur or moneda
            nombre = nombre or nm
    if not estado:
        t = texto.lower()
        if any(k in t for k in ("sin existencias", "agotado", "no está disponible",
                                "no disponible", "sold out", "fuera de stock")):
            estado = "AGOTADO"
        elif any(k in t for k in ("añadir a la cesta", "añadir al carrito", "add to cart",
                                  "comprar", "en stock", "disponible")):
            estado = "DISPONIBLE"
        raw = "texto"
    estado = afina_preventa(estado or "DESCONOCIDO", texto, nombre)
    return {"nombre": nombre, "estado": estado or "DESCONOCIDO", "precio": precio,
            "moneda": moneda or "EUR", "fecha": buscar_fecha(texto), "raw": raw}


def leer_tienda(url):
    if "/products/" in url:
        d = leer_shopify(url)
        if d: return d
    return leer_generico(url)


# ---------- Telegram ----------
def fmt_precio(p, moneda):
    if not p: return ""
    try: s = f"{float(p):.2f}".replace(".", ",")
    except ValueError: s = str(p)
    return f"{s} {({'EUR':'€','USD':'$','GBP':'£'}).get((moneda or '').upper(), moneda or '')}".strip()


def envia_telegram(texto):
    if not TOKEN or not CHAT_ID:
        print("[AVISO] Falta TELEGRAM_TOKEN o TELEGRAM_CHAT_ID; no se envía.")
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                          data={"chat_id": CHAT_ID, "text": texto, "parse_mode": "HTML",
                                "disable_web_page_preview": "false"}, timeout=30)
        if not (r.ok and r.json().get("ok")):
            print("[ERROR Telegram]", r.status_code, r.text[:300]); return False
        return True
    except Exception as e:
        print("[ERROR Telegram]", e); return False


def mensaje(prodinfo, tienda, info, tipo, anterior, mejor):
    emoji = EMOJI.get(info["estado"], "⚪")
    L = ["🆕 <b>Vigilancia iniciada</b>" if tipo == "inicio"
         else f"{emoji} <b>¡Cambio!</b>"]
    L.append(f"<b>{prodinfo.get('nombre', '')}</b>")
    L.append(f"🏬 Tienda: {tienda}")
    if tipo == "cambio" and anterior:
        L.append(f"📦 Estado: {anterior} ➜ {emoji} <b>{info['estado']}</b>")
    else:
        L.append(f"📦 Estado: {emoji} <b>{info['estado']}</b>")
    pr = fmt_precio(info["precio"], info["moneda"])
    if pr: L.append(f"💶 Precio: {pr}")
    if info["fecha"]: L.append(f"📅 Fecha prevista: {info['fecha']}")
    if mejor and mejor["tienda"] != tienda:
        L.append(f"💡 Más barata en stock: {mejor['tienda']} ({fmt_precio(mejor['precio'],'EUR')})")
    L.append(f'🔗 <a href="{info.get("url","")}">Ver en la tienda ▸</a>')
    return "\n".join(L)


# ---------- principal ----------
def targets(modo):
    catalogo = cargar_json(CATALOGO, [])
    by_prod = {p["prod"]: p for p in catalogo}
    if modo == "catalogo":
        return by_prod, list(by_prod.keys())
    watch = cargar_json(PRODUCTOS, [])
    ids = []
    for w in watch:
        pid = w if isinstance(w, str) else (w.get("prod") or w.get("id", "").split("|")[0])
        if pid in by_prod and pid not in ids:
            ids.append(pid)
    return by_prod, ids


def main():
    modo = "catalogo" if "--modo" in sys.argv and "catalogo" in sys.argv else "vigilar"
    by_prod, prods = targets(modo)
    estado = cargar_json(ESTADO, {}); estado.setdefault("_items", {})
    datos = cargar_json(DATOS, {}); datos.setdefault("items", {})
    avisos = 0
    ahora = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    print(f"Modo: {modo} · productos: {len(prods)}")

    for pid in prods:
        p = by_prod.get(pid)
        if not p: continue
        lecturas = []
        for t in p.get("tiendas", []):
            try:
                info = leer_tienda(t["url"])
            except Exception as e:
                print(f"[ERROR {pid}|{t['tienda']}] {e}"); continue
            if not info["nombre"] and not info["precio"] and info["estado"] == "DESCONOCIDO":
                print(f"[AVISO] Lectura vacia {pid}|{t['tienda']}"); continue
            info["url"] = t["url"]
            lecturas.append((t["tienda"], info))
            datos["items"][f"{pid}|{t['tienda']}"] = {
                "estado": info["estado"], "precio": info["precio"], "moneda": info["moneda"],
                "fecha": info["fecha"], "url": t["url"], "visto": ahora}

        # mejor oferta en stock (para el mensaje)
        enstock = [(tt, i) for (tt, i) in lecturas if i["estado"] == "DISPONIBLE" and i["precio"]]
        mejor = None
        if enstock:
            tt, i = min(enstock, key=lambda x: float(x[1]["precio"]))
            mejor = {"tienda": tt, "precio": i["precio"]}

        if modo == "vigilar":
            for tienda, info in lecturas:
                clave = f"{pid}|{tienda}"
                prev = estado["_items"].get(clave)
                print(f"{clave}: {info['estado']} (raw={info['raw']}) {info['precio']}{info['moneda']}")
                if prev is None:
                    if envia_telegram(mensaje(p, tienda, info, "inicio", None, mejor)): avisos += 1
                elif prev.get("estado") != info["estado"] and info["estado"] in ("DISPONIBLE", "PREVENTA"):
                    if envia_telegram(mensaje(p, tienda, info, "cambio", prev.get("estado"), mejor)): avisos += 1
                estado["_items"][clave] = {"estado": info["estado"], "precio": info["precio"],
                                           "fecha": info["fecha"], "visto": ahora}

    datos["_actualizado"] = ahora
    datos["_modo"] = modo
    guardar_json(DATOS, datos)
    if modo == "vigilar":
        estado["_ultima_ejecucion"] = ahora
        guardar_json(ESTADO, estado)
    print(f"Hecho ({modo}). Avisos enviados: {avisos}")


if __name__ == "__main__":
    main()
