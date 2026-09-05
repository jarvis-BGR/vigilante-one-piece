#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Vigilante One Piece — Fase 3 (multi-tienda)
Lee la ficha de cada producto en cada tienda con varias estrategias
(Shopify, meta de producto, JSON-LD, texto) y avisa por Telegram solo
cuando hay un cambio relevante. No compra ni reserva nada: solo informa.
"""
import json, os, re, html, datetime
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
PRODUCTOS = os.path.join(BASE, "productos.json")
ESTADO = os.path.join(BASE, "estado.json")

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


# ---------- utilidades de lectura ----------
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
    s = str(valor).strip()
    m = re.search(r'(\d+[.,]?\d*)', s)
    return m.group(1).replace(",", ".") if m else ""


def json_ld_producto(contenido):
    """Devuelve (availability, price, currency, name) desde JSON-LD si hay Product."""
    for m in re.finditer(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                         contenido, re.I | re.S):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        candidatos = data if isinstance(data, list) else \
            (data.get("@graph", [data]) if isinstance(data, dict) else [])
        for obj in candidatos:
            if not isinstance(obj, dict):
                continue
            t = obj.get("@type", "")
            if (t == "Product" or (isinstance(t, list) and "Product" in t)) and obj.get("offers"):
                offers = obj["offers"]
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                return (str(offers.get("availability", "")),
                        precio_num(offers.get("price")),
                        offers.get("priceCurrency", ""),
                        obj.get("name", ""))
    return ("", "", "", "")


def estado_desde_schema(av):
    a = (av or "").lower()
    if "preorder" in a or "backorder" in a:
        return "PREVENTA"
    if "outofstock" in a or "soldout" in a or "discontinued" in a:
        return "AGOTADO"
    if "instock" in a or "onlineonly" in a or "limitedavailability" in a:
        return "DISPONIBLE"
    return ""


def estado_desde_meta(av):
    a = (av or "").strip().lower()
    return {"in stock": "DISPONIBLE", "instock": "DISPONIBLE",
            "available for order": "PREVENTA", "preorder": "PREVENTA",
            "backorder": "PREVENTA", "out of stock": "AGOTADO",
            "discontinued": "AGOTADO"}.get(a, "")


def afina_preventa(estado, texto, titulo=""):
    """Si algo está 'disponible' pero la ficha grita preventa, es PREVENTA."""
    if estado == "DISPONIBLE":
        t = (texto + " " + (titulo or "")).lower()
        if any(k in t for k in PREVENTA_KW):
            return "PREVENTA"
    return estado


# ---------- estrategias por tienda ----------
def leer_shopify(url):
    base = url.split("?")[0].rstrip("/")
    try:
        r = http_get(base + ".json")
        if not r.ok:
            return None
        prod = r.json().get("product")
        if not prod:
            return None
    except Exception:
        return None
    variants = prod.get("variants", []) or []
    disponible = any(v.get("available") for v in variants)
    precio = precio_num(variants[0].get("price")) if variants else ""
    nombre = prod.get("title", "")
    estado = "DISPONIBLE" if disponible else "AGOTADO"
    # matiz de preventa desde el HTML
    fecha, texto = "", ""
    try:
        h = http_get(base).text
        texto = re.sub(r"<[^>]+>", " ", h)
        estado = afina_preventa(estado, texto, nombre)
        fecha = buscar_fecha(texto)
    except Exception:
        pass
    return {"nombre": nombre, "estado": estado, "precio": precio,
            "moneda": "EUR", "fecha": fecha, "raw": "shopify:" + str(disponible)}


def leer_generico(url):
    r = http_get(url)
    r.raise_for_status()
    contenido = r.text
    texto = re.sub(r"<[^>]+>", " ", contenido)
    metas = leer_metas(contenido)
    nombre = metas.get("og:title", "")
    precio = precio_num(metas.get("product:price:amount"))
    moneda = metas.get("product:price:currency", "EUR")

    # 1) meta de producto (Comic Stores)
    estado = estado_desde_meta(metas.get("product:availability", ""))
    raw = "meta:" + metas.get("product:availability", "")

    # 2) JSON-LD
    if not estado:
        av, p, cur, nm = json_ld_producto(contenido)
        e = estado_desde_schema(av)
        if e:
            estado, raw = e, "jsonld:" + av
            if not precio:
                precio = p
            if cur:
                moneda = cur
            if not nombre:
                nombre = nm

    # 3) texto visible (último recurso)
    if not estado:
        t = texto.lower()
        if any(k in t for k in ("sin existencias", "agotado", "no está disponible",
                                "no disponible", "sold out", "fuera de stock")):
            estado = "AGOTADO"
        elif any(k in t for k in ("añadir a la cesta", "añadir al carrito",
                                  "add to cart", "comprar", "en stock", "disponible")):
            estado = "DISPONIBLE"
        raw = "texto"

    estado = afina_preventa(estado or "DESCONOCIDO", texto, nombre)
    return {"nombre": nombre, "estado": estado or "DESCONOCIDO", "precio": precio,
            "moneda": moneda or "EUR", "fecha": buscar_fecha(texto), "raw": raw}


def leer_tienda(url):
    if "/products/" in url:           # patrón típico de Shopify
        data = leer_shopify(url)
        if data:
            return data
    return leer_generico(url)


# ---------- Telegram ----------
def formatea_precio(p, moneda):
    if not p:
        return ""
    try:
        s = f"{float(p):.2f}".replace(".", ",")
    except ValueError:
        s = str(p)
    return f"{s} {({'EUR':'€','USD':'$','GBP':'£'}).get((moneda or '').upper(), moneda or '')}".strip()


def mensaje(item, info, tipo, anterior=None):
    emoji = EMOJI.get(info["estado"], "⚪")
    L = ["🆕 <b>Vigilancia iniciada</b>" if tipo == "inicio"
         else f"{emoji} <b>¡Cambio de estado!</b>"]
    L.append(f"<b>{info['nombre'] or item.get('nombre', '')}</b>")
    L.append(f"🏬 Tienda: {item['tienda']}")
    if tipo == "cambio" and anterior:
        L.append(f"📦 Estado: {anterior} ➜ {emoji} <b>{info['estado']}</b>")
    else:
        L.append(f"📦 Estado: {emoji} <b>{info['estado']}</b>")
    precio = formatea_precio(info["precio"], info["moneda"])
    if precio:
        L.append(f"💶 Precio: {precio}")
    if info["fecha"]:
        L.append(f"📅 Fecha prevista: {info['fecha']}")
    L.append(f'🔗 <a href="{item["url"]}">Ver en la tienda ▸</a>')
    return "\n".join(L)


def envia_telegram(texto):
    if not TOKEN or not CHAT_ID:
        print("[AVISO] Falta TELEGRAM_TOKEN o TELEGRAM_CHAT_ID; no se envía.")
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                          data={"chat_id": CHAT_ID, "text": texto, "parse_mode": "HTML",
                                "disable_web_page_preview": "false"}, timeout=30)
        if not (r.ok and r.json().get("ok")):
            print("[ERROR Telegram]", r.status_code, r.text[:300])
            return False
        return True
    except Exception as e:
        print("[ERROR Telegram]", e)
        return False


def main():
    productos = cargar_json(PRODUCTOS, [])
    estado = cargar_json(ESTADO, {})
    estado.setdefault("_items", {})
    cambios = 0
    for item in productos:
        clave = item.get("id") or f'{item.get("tienda","")}|{item.get("ean", "")}'
        try:
            info = leer_tienda(item["url"])
        except Exception as e:
            print(f"[ERROR leyendo {clave}] {e}")
            continue
        if not info["nombre"] and not info["precio"] and info["estado"] == "DESCONOCIDO":
            print(f"[AVISO] Lectura vacia o bloqueada en {clave}; no se actualiza.")
            continue
        prev = estado["_items"].get(clave)
        print(f"{clave}: {info['estado']} (raw='{info['raw']}') "
              f"precio={info['precio']}{info['moneda']} fecha='{info['fecha']}'")
        if prev is None:
            if envia_telegram(mensaje(item, info, "inicio")):
                cambios += 1
        elif prev.get("estado") != info["estado"]:
            if envia_telegram(mensaje(item, info, "cambio", anterior=prev.get("estado"))):
                cambios += 1
        estado["_items"][clave] = {"estado": info["estado"], "precio": info["precio"],
                                   "fecha": info["fecha"],
                                   "nombre": info["nombre"] or item.get("nombre", ""),
                                   "tienda": item.get("tienda", ""), "url": item.get("url", ""),
                                   "visto": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}
    estado["_ultima_ejecucion"] = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    guardar_json(ESTADO, estado)
    print(f"Hecho. Avisos enviados: {cambios}")


if __name__ == "__main__":
    main()
