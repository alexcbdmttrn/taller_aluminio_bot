import os
import json
import re
import logging
import asyncio
import io
import tempfile
from decimal import Decimal
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
import pytz
import asyncpg
from openai import AsyncOpenAI
from groq import Groq
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    JobQueue,
)

# ==================== CONFIGURACIÓN ====================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

if not all([TOKEN, DATABASE_URL, DEEPSEEK_API_KEY]):
    logger.error("❌ Faltan variables de entorno esenciales.")
    exit(1)

deepseek_client = AsyncOpenAI(
    api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com", timeout=60.0
)
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

try:
    ZONA_HORARIA = pytz.timezone("America/Mexico_City")
except Exception:
    ZONA_HORARIA = None


def ahora_cdmx():
    if ZONA_HORARIA:
        return datetime.now(ZONA_HORARIA)
    return datetime.utcnow() - timedelta(hours=6)


def local_a_utc(fecha_local: datetime) -> datetime:
    if ZONA_HORARIA:
        fecha_localizada = ZONA_HORARIA.localize(fecha_local)
        fecha_utc = fecha_localizada.astimezone(pytz.UTC)
        return fecha_utc.replace(tzinfo=None)
    return fecha_local + timedelta(hours=6)


def utc_a_local(fecha_utc: datetime) -> datetime:
    if ZONA_HORARIA:
        fecha_utc_localized = pytz.UTC.localize(fecha_utc)
        return fecha_utc_localized.astimezone(ZONA_HORARIA).replace(tzinfo=None)
    return fecha_utc - timedelta(hours=6)


# ==================== POOL DE CONEXIONES ====================
_db_pool = None


async def init_db_pool():
    global _db_pool
    try:
        _db_pool = await asyncpg.create_pool(
            DATABASE_URL, min_size=1, max_size=20, command_timeout=30
        )
        logger.info("✅ Pool de conexiones asyncpg iniciado.")
    except Exception as e:
        logger.error(f"❌ Error al crear pool asyncpg: {e}")
        raise


async def get_connection():
    return await _db_pool.acquire()


async def put_connection(conn):
    await _db_pool.release(conn)


async def ejecutar_query(query, params=None, fetch=False):
    conn = await get_connection()
    try:
        if fetch:
            return await conn.fetch(query, *(params or ()))
        else:
            return await conn.execute(query, *(params or ()))
    finally:
        await put_connection(conn)


# ==================== CREAR TABLAS ====================
async def crear_tablas():
    queries = [
        """
        CREATE TABLE IF NOT EXISTS clientes (
            id SERIAL PRIMARY KEY,
            nombre TEXT NOT NULL,
            telefono TEXT,
            direccion TEXT,
            notas_adicionales TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS proyectos (
            id SERIAL PRIMARY KEY,
            cliente_id INTEGER REFERENCES clientes(id) ON DELETE CASCADE,
            nombre_corto TEXT,
            descripcion TEXT,
            monto_total DECIMAL(10,2) DEFAULT 0,
            monto_pagado DECIMAL(10,2) DEFAULT 0,
            estado TEXT DEFAULT 'Pendiente de cotizar',
            fecha_creacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            notas_adicionales TEXT,
            material_comprado BOOLEAN DEFAULT FALSE,
            fecha_compra_material TIMESTAMP,
            costo_material DECIMAL(10,2),
            presupuesto_enviado BOOLEAN DEFAULT FALSE,
            fecha_presupuesto TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS gastos (
            id SERIAL PRIMARY KEY,
            descripcion TEXT NOT NULL,
            monto DECIMAL(10,2) NOT NULL,
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS recordatorios (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            mensaje TEXT NOT NULL,
            fecha_recordatorio TIMESTAMP NOT NULL,
            enviado BOOLEAN DEFAULT FALSE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS historial_chat (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            rol TEXT NOT NULL,
            contenido TEXT NOT NULL,
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        "CREATE EXTENSION IF NOT EXISTS unaccent",
    ]
    for query in queries:
        try:
            await ejecutar_query(query)
        except Exception as e:
            logger.warning(f"⚠️ Error creando tabla: {e}")


# ==================== HISTORIAL EN POSTGRES ====================
async def guardar_historial(chat_id: int, mensaje: dict):
    query = """
        INSERT INTO historial_chat (chat_id, rol, contenido)
        VALUES ($1, $2, $3)
    """
    await ejecutar_query(query, (chat_id, mensaje.get("role", "user"), json.dumps(mensaje)))


async def obtener_historial(chat_id: int, limite: int = 20) -> List[Dict]:
    query = """
        SELECT contenido
        FROM historial_chat
        WHERE chat_id = $1
        ORDER BY fecha DESC, id DESC
        LIMIT $2
    """
    resultados = await ejecutar_query(query, (chat_id, limite), fetch=True)
    mensajes = []
    for r in reversed(resultados):
        try:
            m = json.loads(r["contenido"])
        except json.JSONDecodeError:
            m = {"role": "assistant", "content": r["contenido"]}
        if not isinstance(m, dict) or "role" not in m:
            logger.warning(f"Fila de historial descartada por formato inválido: {m}")
            continue
        mensajes.append(m)
    return mensajes


async def limpiar_historial(chat_id: int):
    await ejecutar_query("DELETE FROM historial_chat WHERE chat_id = $1", (chat_id,))


# ==================== CLIENTES Y PROYECTOS ====================
async def buscar_o_crear_cliente(nombre_cliente, telefono=None, direccion=None, notas=None):
    nombre_cliente = nombre_cliente.lower().strip()
    query = "SELECT id FROM clientes WHERE unaccent(nombre) ILIKE unaccent($1)"
    result = await ejecutar_query(query, (f"%{nombre_cliente}%",), fetch=True)
    if result:
        cliente_id = result[0]["id"]
        if telefono or direccion or notas:
            updates, params = [], []
            if telefono:
                updates.append(f"telefono = ${len(params)+1}")
                params.append(telefono)
            if direccion:
                updates.append(f"direccion = ${len(params)+1}")
                params.append(direccion)
            if notas:
                updates.append(f"notas_adicionales = ${len(params)+1}")
                params.append(notas)
            if updates:
                params.append(cliente_id)
                set_clause = ", ".join(updates)
                await ejecutar_query(f"UPDATE clientes SET {set_clause} WHERE id = ${len(params)}", params)
        return cliente_id
    else:
        insert = """
            INSERT INTO clientes (nombre, telefono, direccion, notas_adicionales)
            VALUES ($1, $2, $3, $4) RETURNING id
        """
        result = await ejecutar_query(insert, (nombre_cliente, telefono, direccion, notas), fetch=True)
        return result[0]["id"]


async def obtener_proyectos_activos(cliente_nombre):
    cliente_nombre = cliente_nombre.lower().strip()
    query = """
        SELECT p.id, p.nombre_corto, p.descripcion, p.monto_total, p.monto_pagado, p.estado,
               p.material_comprado, p.costo_material, p.presupuesto_enviado, c.nombre as cliente
        FROM proyectos p JOIN clientes c ON p.cliente_id = c.id
        WHERE unaccent(c.nombre) ILIKE unaccent($1) AND p.estado NOT IN ('Liquidado', 'Cancelado')
        ORDER BY p.fecha_creacion DESC
    """
    return await ejecutar_query(query, (f"%{cliente_nombre}%",), fetch=True)


# ==================== DESAMBIGUACIÓN ====================
async def _resolver_proyecto_o_pedir(
    cliente: str,
    nombre_corto: Optional[str],
    proyectos: List[Dict],
    accion_descripcion: str = "",
) -> Tuple[Optional[Dict], Optional[Dict]]:
    if not proyectos:
        return None, {"exito": False, "error": f"No hay proyectos activos para {cliente}."}
    if nombre_corto:
        coincidencias = [p for p in proyectos if nombre_corto.lower() in (p["nombre_corto"] or "").lower()]
        if len(coincidencias) == 1:
            return coincidencias[0], None
        if len(coincidencias) > 1:
            proyectos = coincidencias
        elif len(coincidencias) == 0:
            coincidencias = [p for p in proyectos if nombre_corto.lower() in (p["descripcion"] or "").lower()]
            if len(coincidencias) == 1:
                return coincidencias[0], None
            if len(coincidencias) > 1:
                proyectos = coincidencias
            else:
                return None, {"exito": False, "error": f"No encontré proyecto para '{nombre_corto}' en {cliente}."}
    if len(proyectos) == 1:
        return proyectos[0], None
    opciones = []
    for p in proyectos:
        nombre = p["nombre_corto"] or "Proyecto"
        monto = float(p["monto_total"])
        estado = p["estado"]
        opciones.append(f"'{nombre}' (${monto:.2f}, {estado})")
    return None, {
        "exito": False,
        "requiere_seleccion": True,
        "opciones": opciones,
        "error": f"{cliente} tiene {len(proyectos)} proyectos activos. Pregunta cuál y vuelve a llamar con 'nombre_corto'.",
    }


# ==================== TOOLS ====================
async def tool_registrar_proyecto(
    cliente: str,
    nombre_corto: str,
    descripcion: str,
    monto: float,
    telefono: str = None,
    direccion: str = None,
    notas: str = None,
):
    if monto < 0:
        return {"exito": False, "error": "El monto no puede ser negativo."}
    cliente_id = await buscar_o_crear_cliente(cliente, telefono, direccion, notas)
    insert = """
        INSERT INTO proyectos (cliente_id, nombre_corto, descripcion, monto_total, monto_pagado, estado, notas_adicionales)
        VALUES ($1, $2, $3, $4, 0, 'Pendiente de cotizar', $5) RETURNING id
    """
    result = await ejecutar_query(insert, (cliente_id, nombre_corto or "Proyecto General", descripcion, monto, notas), fetch=True)
    return {"exito": True, "mensaje": f"Proyecto '{nombre_corto}' registrado para {cliente}. ID: {result[0]['id']}"}


async def tool_registrar_pago(cliente: str, monto: float, referencia: str = None):
    if monto <= 0:
        return {"exito": False, "error": "El monto debe ser mayor a cero."}
    proyectos = await obtener_proyectos_activos(cliente)
    if not proyectos:
        return {"exito": False, "error": f"No hay proyectos activos para {cliente}."}
    proyecto, aviso = await _resolver_proyecto_o_pedir(cliente, referencia, proyectos, "registrar pago")
    if aviso:
        return aviso
    pid = proyecto["id"]
    nc = proyecto["nombre_corto"]
    total = proyecto["monto_total"]
    pagado = proyecto["monto_pagado"]
    estado = proyecto["estado"]
    nuevo_pagado = Decimal(str(pagado)) + Decimal(str(monto))
    saldo = max(Decimal("0"), Decimal(str(total)) - nuevo_pagado)
    nuevo_estado = "Liquidado" if saldo == 0 else "Por cobrar"
    if monto > 0 and estado == "Pendiente de cotizar":
        nuevo_estado = "En proceso"
    await ejecutar_query(
        "UPDATE proyectos SET monto_pagado = $1, estado = $2 WHERE id = $3",
        (float(nuevo_pagado), nuevo_estado, pid),
    )
    return {
        "exito": True,
        "mensaje": f"Pago de ${monto:.2f} registrado para '{nc}' de {cliente}. Saldo restante: ${float(saldo):.2f}. Estado: {nuevo_estado}",
    }


async def tool_marcar_presupuesto_enviado(
    cliente: str,
    nombre_corto: str = None,
    monto: float = None,
    descripcion: str = None,
):
    proyectos = await obtener_proyectos_activos(cliente)
    if not proyectos:
        return {"exito": False, "error": f"No hay proyectos activos para {cliente}."}
    proyecto, aviso = await _resolver_proyecto_o_pedir(cliente, nombre_corto, proyectos, "marcar presupuesto enviado")
    if aviso:
        return aviso
    pid = proyecto["id"]
    nc = proyecto["nombre_corto"]
    estado_actual = proyecto["estado"]
    updates = ["presupuesto_enviado = TRUE", "fecha_presupuesto = CURRENT_TIMESTAMP"]
    params = []
    if estado_actual == "Pendiente de cotizar":
        updates.append("estado = 'Presupuesto enviado'")
    if monto is not None and monto > 0:
        updates.append(f"monto_total = ${len(params)+1}")
        params.append(monto)
    if descripcion:
        updates.append(f"descripcion = COALESCE(${len(params)+1}, descripcion)")
        params.append(descripcion)
    params.append(pid)
    await ejecutar_query(f"UPDATE proyectos SET {', '.join(updates)} WHERE id = ${len(params)}", params)
    return {"exito": True, "mensaje": f"Presupuesto marcado como enviado para '{nc}' de {cliente}."}


async def tool_marcar_material_comprado(
    cliente: str,
    nombre_corto: str = None,
    comprado: bool = True,
    costo_material: float = None,
):
    proyectos = await obtener_proyectos_activos(cliente)
    if not proyectos:
        return {"exito": False, "error": f"No hay proyectos activos para {cliente}."}
    proyecto, aviso = await _resolver_proyecto_o_pedir(cliente, nombre_corto, proyectos, "marcar material comprado")
    if aviso:
        return aviso
    pid = proyecto["id"]
    nc = proyecto["nombre_corto"]
    updates = ["material_comprado = $1"]
    params: list = [comprado]
    if comprado:
        updates.append("fecha_compra_material = CURRENT_TIMESTAMP")
    else:
        updates.append("fecha_compra_material = NULL")
    if costo_material is not None and costo_material > 0:
        updates.append(f"costo_material = ${len(params)+1}")
        params.append(costo_material)
    elif not comprado:
        updates.append(f"costo_material = ${len(params)+1}")
        params.append(None)
    params.append(pid)
    await ejecutar_query(f"UPDATE proyectos SET {', '.join(updates)} WHERE id = ${len(params)}", params)
    estado_texto = "comprado ✅" if comprado else "pendiente ⏳"
    costo_texto = f" (costo: ${costo_material:.2f})" if (comprado and costo_material) else ""
    return {
        "exito": True,
        "mensaje": f"Material de '{nc}' de {cliente} marcado como {estado_texto}{costo_texto}.",
    }


async def tool_consultar_proyectos(tipo: str = "activos", cliente: str = None):
    if tipo == "activos":
        condicion = "p.estado NOT IN ('Liquidado', 'Cancelado')"
    elif tipo == "liquidados":
        condicion = "p.estado = 'Liquidado'"
    elif tipo == "cancelados":
        condicion = "p.estado = 'Cancelado'"
    elif tipo == "deudores":
        condicion = "p.estado IN ('Por cobrar', 'En proceso') AND p.monto_total > p.monto_pagado"
    else:
        return {"exito": False, "error": "Tipo de consulta no válido."}
    if cliente:
        cliente = cliente.lower().strip()
        count_query = f"SELECT COUNT(*) FROM proyectos p JOIN clientes c ON p.cliente_id = c.id WHERE unaccent(c.nombre) ILIKE unaccent($1) AND {condicion}"
        total_count = await ejecutar_query(count_query, (f"%{cliente}%",), fetch=True)
        total_real = total_count[0]["count"] if total_count else 0
        query = f"""
            SELECT c.nombre, p.nombre_corto, p.descripcion, p.monto_total, p.monto_pagado, p.estado,
                   p.material_comprado, p.costo_material, p.presupuesto_enviado, c.telefono, c.direccion
            FROM proyectos p JOIN clientes c ON p.cliente_id = c.id
            WHERE unaccent(c.nombre) ILIKE unaccent($1) AND {condicion}
            ORDER BY p.fecha_creacion DESC LIMIT 5
        """
        resultados = await ejecutar_query(query, (f"%{cliente}%",), fetch=True)
    else:
        count_query = f"SELECT COUNT(*) FROM proyectos p WHERE {condicion}"
        total_count = await ejecutar_query(count_query, fetch=True)
        total_real = total_count[0]["count"] if total_count else 0
        query = f"""
            SELECT c.nombre, p.nombre_corto, p.descripcion, p.monto_total, p.monto_pagado, p.estado,
                   p.material_comprado, p.costo_material, p.presupuesto_enviado, c.telefono, c.direccion
            FROM proyectos p JOIN clientes c ON p.cliente_id = c.id
            WHERE {condicion}
            ORDER BY c.nombre, p.fecha_creacion DESC LIMIT 5
        """
        resultados = await ejecutar_query(query, fetch=True)
    if not resultados:
        return {"exito": True, "mensaje": "No hay proyectos en este estado.", "data": []}
    data = []
    for row in resultados:
        data.append({
            "cliente": row["nombre"],
            "proyecto": row["nombre_corto"] or "General",
            "descripcion": row["descripcion"],
            "total": float(row["monto_total"]),
            "pagado": float(row["monto_pagado"]),
            "saldo": float(row["monto_total"]) - float(row["monto_pagado"]),
            "estado": row["estado"],
            "material_comprado": row["material_comprado"],
            "costo_material": float(row["costo_material"]) if row["costo_material"] else None,
            "presupuesto_enviado": row["presupuesto_enviado"],
            "telefono": row["telefono"],
            "direccion": row["direccion"],
        })
    mensaje = f"Mostrando {len(data)} de {total_real} proyectos." if total_real > len(data) else None
    return {"exito": True, "data": data, "mensaje": mensaje}


async def tool_cerrar_proyecto(cliente: str, nombre_corto: str = None):
    proyectos = await obtener_proyectos_activos(cliente)
    if not proyectos:
        return {"exito": False, "error": f"No hay proyectos activos para {cliente}."}
    proyecto, aviso = await _resolver_proyecto_o_pedir(cliente, nombre_corto, proyectos, "cerrar/liquidar proyecto")
    if aviso:
        return aviso
    pid = proyecto["id"]
    nc = proyecto["nombre_corto"]
    total = proyecto["monto_total"]
    pagado = proyecto["monto_pagado"]
    saldo = total - pagado
    if saldo <= 0:
        await ejecutar_query("UPDATE proyectos SET estado = 'Liquidado' WHERE id = $1", (pid,))
        return {"exito": True, "mensaje": f"Proyecto '{nc}' de {cliente} liquidado (saldo $0)."}
    else:
        return {"exito": False, "error": f"El proyecto '{nc}' tiene saldo pendiente de ${saldo:.2f}. Primero registra el pago restante."}


async def tool_cancelar_proyecto(cliente: str, nombre_corto: str = None):
    query = """
        SELECT p.id, p.nombre_corto, p.descripcion, p.monto_total, p.monto_pagado, p.estado,
               p.material_comprado, p.costo_material, p.presupuesto_enviado, c.nombre as cliente
        FROM proyectos p JOIN clientes c ON p.cliente_id = c.id
        WHERE unaccent(c.nombre) ILIKE unaccent($1) AND p.estado != 'Cancelado'
        ORDER BY p.fecha_creacion DESC
    """
    proyectos_raw = await ejecutar_query(query, (f"%{cliente}%",), fetch=True)
    if not proyectos_raw:
        return {"exito": False, "error": f"No hay proyectos para {cliente} que no estén cancelados."}
    proyectos = [dict(row) for row in proyectos_raw]
    proyecto, aviso = await _resolver_proyecto_o_pedir(cliente, nombre_corto, proyectos, "cancelar proyecto")
    if aviso:
        return aviso
    pid = proyecto["id"]
    nc = proyecto["nombre_corto"]
    await ejecutar_query("UPDATE proyectos SET estado = 'Cancelado' WHERE id = $1", (pid,))
    return {"exito": True, "mensaje": f"Proyecto '{nc}' de {cliente} ha sido cancelado."}


async def tool_borrar_proyecto(cliente: str, nombre_corto: str = None, confirmado: bool = False):
    if not confirmado:
        return {"exito": False, "error": "Se requiere confirmación explícita para borrar. Responde 'SÍ' para confirmar."}
    query = """
        SELECT p.id, p.nombre_corto, p.descripcion, p.monto_total, p.monto_pagado, p.estado,
               p.material_comprado, p.costo_material, p.presupuesto_enviado, c.nombre as cliente
        FROM proyectos p JOIN clientes c ON p.cliente_id = c.id
        WHERE unaccent(c.nombre) ILIKE unaccent($1)
        ORDER BY p.fecha_creacion DESC
    """
    proyectos_raw = await ejecutar_query(query, (f"%{cliente}%",), fetch=True)
    if not proyectos_raw:
        return {"exito": False, "error": f"No encontré proyectos para {cliente}."}
    proyectos = [dict(row) for row in proyectos_raw]
    proyecto, aviso = await _resolver_proyecto_o_pedir(cliente, nombre_corto, proyectos, "borrar proyecto")
    if aviso:
        return aviso
    pid = proyecto["id"]
    nc = proyecto["nombre_corto"]
    await ejecutar_query("DELETE FROM proyectos WHERE id = $1", (pid,))
    await ejecutar_query("DELETE FROM clientes WHERE id NOT IN (SELECT DISTINCT cliente_id FROM proyectos WHERE cliente_id IS NOT NULL)")
    return {"exito": True, "mensaje": f"Proyecto '{nc}' de {cliente} eliminado definitivamente."}


async def tool_registrar_gasto(descripcion: str, monto: float):
    if monto <= 0:
        return {"exito": False, "error": "El monto debe ser mayor a cero."}
    await ejecutar_query("INSERT INTO gastos (descripcion, monto) VALUES ($1, $2)", (descripcion, monto))
    return {"exito": True, "mensaje": f"Gasto '{descripcion}' de ${monto:.2f} registrado."}


async def tool_consultar_gastos(limite: int = 10):
    resultados = await ejecutar_query(
        "SELECT fecha, descripcion, monto FROM gastos ORDER BY fecha DESC LIMIT $1",
        (limite,), fetch=True
    )
    if not resultados:
        return {"exito": True, "data": [], "mensaje": "No hay gastos registrados."}
    data = [{"fecha": r["fecha"].strftime("%d/%m %H:%M"), "descripcion": r["descripcion"], "monto": float(r["monto"])} for r in resultados]
    return {"exito": True, "data": data}


async def tool_explicar_estado(cliente: str, nombre_corto: str = None):
    proyectos = await obtener_proyectos_activos(cliente)
    if not proyectos:
        return {"exito": False, "error": f"No hay proyectos activos para {cliente}."}
    proyecto, aviso = await _resolver_proyecto_o_pedir(cliente, nombre_corto, proyectos, "explicar estado")
    if aviso:
        return aviso
    nc = proyecto["nombre_corto"]
    total = proyecto["monto_total"]
    pagado = proyecto["monto_pagado"]
    estado = proyecto["estado"]
    saldo = total - pagado
    explicacion = f"Proyecto '{nc}' de {cliente}: Estado '{estado}', Monto total ${total:.2f}, Pagado ${pagado:.2f}, Saldo ${saldo:.2f}."
    if estado == "Liquidado":
        explicacion += " Está liquidado porque el saldo es cero."
    elif estado == "Por cobrar":
        explicacion += " Está pendiente de cobro porque el saldo es positivo."
    elif estado == "En proceso":
        explicacion += " Está en proceso (se ha recibido algún anticipo)."
    return {"exito": True, "mensaje": explicacion}


async def tool_editar_cliente(cliente: str, telefono: str = None, direccion: str = None, notas: str = None):
    query_buscar = "SELECT id, nombre FROM clientes WHERE unaccent(nombre) ILIKE unaccent($1)"
    resultado = await ejecutar_query(query_buscar, (f"%{cliente.lower().strip()}%",), fetch=True)
    if not resultado:
        return {"exito": False, "error": f"No encontré un cliente llamado '{cliente}'."}
    cliente_id = resultado[0]["id"]
    nombre_real = resultado[0]["nombre"]
    updates, params = [], []
    if telefono is not None:
        updates.append(f"telefono = ${len(params)+1}")
        params.append(telefono)
    if direccion is not None:
        updates.append(f"direccion = ${len(params)+1}")
        params.append(direccion)
    if notas is not None:
        updates.append(f"notas_adicionales = ${len(params)+1}")
        params.append(notas)
    if not updates:
        return {"exito": False, "error": "No se proporcionaron datos nuevos para actualizar."}
    params.append(cliente_id)
    await ejecutar_query(f"UPDATE clientes SET {', '.join(updates)} WHERE id = ${len(params)}", params)
    return {"exito": True, "mensaje": f"Datos actualizados correctamente para el cliente '{nombre_real}'."}


# ==================== INTERPRETACIÓN DE FECHAS ====================
def interpretar_fecha(fecha_texto: str) -> Tuple[Optional[str], Optional[str], Optional[datetime]]:
    """Interpreta fechas informales. Devuelve (fecha_normalizada, mensaje_ambiguedad, fecha_local)."""
    fecha_texto = fecha_texto.lower().strip()
    hoy = ahora_cdmx()
    fecha_actual = hoy.strftime("%Y-%m-%d")
    fecha_manana_dt = hoy + timedelta(days=1)
    fecha_manana = fecha_manana_dt.strftime("%Y-%m-%d")

    texto = fecha_texto.replace("hoy", fecha_actual).replace("mañana", fecha_manana)
    texto = re.sub(r'\ba\s*(?:las|la)\s*', '', texto)
    texto = re.sub(r'\bcon\b', '', texto)
    texto = re.sub(r'\bminutos?\b', '', texto)
    texto = re.sub(r'\bpara\b', '', texto)

    numeros = re.findall(r'\d+', texto)
    if not numeros:
        return None, "No encontré una hora. Por favor, especifica la hora (ej. '2:30 AM').", None

    es_manana = 'mañana' in fecha_texto or 'madrugada' in fecha_texto or 'am' in fecha_texto
    es_tarde = 'tarde' in fecha_texto or 'pm' in fecha_texto
    es_noche = 'noche' in fecha_texto

    hora = None
    minuto = 0
    segundo = 0

    if ' y ' in fecha_texto:
        partes = fecha_texto.split(' y ')
        if len(partes) >= 2:
            nums = re.findall(r'\d+', partes[0])
            if nums: hora = int(nums[-1])
            nums2 = re.findall(r'\d+', partes[1])
            if nums2: minuto = int(nums2[0])
    elif ' con ' in fecha_texto:
        partes = fecha_texto.split(' con ')
        if len(partes) >= 2:
            nums = re.findall(r'\d+', partes[0])
            if nums: hora = int(nums[-1])
            nums2 = re.findall(r'\d+', partes[1])
            if nums2: minuto = int(nums2[0])
    else:
        hora_match = re.search(r'(\d{1,2})\s*[:.,]\s*(\d{2})', fecha_texto)
        if hora_match:
            hora = int(hora_match.group(1))
            minuto = int(hora_match.group(2))
        else:
            nums = re.findall(r'\d+', fecha_texto)
            if len(nums) >= 2:
                hora = int(nums[-2])
                minuto = int(nums[-1])
            elif len(nums) == 1:
                hora = int(nums[0])
                minuto = 0

    if hora is None:
        return None, "No entendí la hora. Por favor, especifica una hora como '2:30' o '2 y 30'.", None

    if es_manana:
        if hora == 12: hora = 0
    elif es_tarde or es_noche:
        if hora < 12: hora += 12
    else:
        if hora < 6:
            return None, f"¿Quieres decir {hora:02d}:{minuto:02d} AM o {hora+12:02d}:{minuto:02d} PM?", None

    if hora > 23: hora = 12
    if minuto > 59: minuto = 0

    fecha_match = re.search(r'(\d{4}-\d{2}-\d{2})', texto)
    fecha_str = fecha_match.group(1) if fecha_match else fecha_actual
    fecha_normalizada = f"{fecha_str} {hora:02d}:{minuto:02d}:{segundo:02d}"

    try:
        fecha_local = datetime.strptime(fecha_normalizada, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None, "Formato de fecha inválido.", None

    return fecha_normalizada, None, fecha_local


async def tool_crear_recordatorio(mensaje: str, fecha_recordatorio: str, chat_id: int):
    try:
        fecha_normalizada, ambiguedad, fecha_local = interpretar_fecha(fecha_recordatorio)
        if not fecha_normalizada:
            if ambiguedad:
                return {
                    "exito": False,
                    "requiere_aclaracion_fecha": True,
                    "error": ambiguedad,
                    "mensaje_original": mensaje,
                    "fecha_ambigua": fecha_recordatorio,
                }
            return {"exito": False, "error": f"⚠️ No pude interpretar la fecha: '{fecha_recordatorio}'."}

        logger.info(f"📥 Fecha recibida: '{fecha_recordatorio}' -> interpretada: '{fecha_normalizada}'")
        fecha_utc = local_a_utc(fecha_local)
        ahora_utc = datetime.utcnow()
        diferencia = (fecha_utc - ahora_utc).total_seconds()

        if diferencia < -120:
            fecha_mostrar = fecha_local.strftime("%d/%m/%Y %I:%M %p")
            fecha_manana = fecha_local + timedelta(days=1)
            fecha_manana_str = fecha_manana.strftime("%d/%m/%Y %I:%M %p")
            return {
                "exito": False,
                "requiere_confirmacion": True,
                "fecha_local": fecha_mostrar,
                "mensaje": mensaje,
                "error": f"⚠️ La fecha programada ({fecha_mostrar}) ya pasó. ¿Quieres programarlo para mañana ({fecha_manana_str}) o confirmas la fecha actual?",
                "datos_originales": {
                    "mensaje": mensaje,
                    "fecha_local": fecha_local.isoformat(),
                    "fecha_utc": fecha_utc.isoformat()
                }
            }
        elif diferencia > 180:
            query = """
                INSERT INTO recordatorios (chat_id, mensaje, fecha_recordatorio, enviado)
                VALUES ($1, $2, $3, FALSE) RETURNING id
            """
            result = await ejecutar_query(query, (chat_id, mensaje, fecha_utc), fetch=True)
            nuevo_id = result[0]["id"]
            fecha_mostrar = fecha_local.strftime("%d/%m/%Y %I:%M %p")
            return {"exito": True, "mensaje": f"🔔 Nuevo recordatorio programado para el {fecha_mostrar}.\n\n📝 *{mensaje}*"}
        else:
            fecha_mostrar = fecha_local.strftime("%d/%m/%Y %I:%M %p")
            hora_actual = ahora_cdmx().strftime("%I:%M %p")
            return {
                "exito": False,
                "requiere_confirmacion": True,
                "fecha_local": fecha_mostrar,
                "mensaje": mensaje,
                "error": f"⚠️ La fecha programada ({fecha_mostrar}) es muy cercana. La hora actual es {hora_actual}. ¿Confirmas? Responde 'sí' o 'no'.",
                "datos_originales": {
                    "mensaje": mensaje,
                    "fecha_local": fecha_local.isoformat(),
                    "fecha_utc": fecha_utc.isoformat()
                }
            }
    except Exception as e:
        logger.error(f"Error en tool_crear_recordatorio: {e}")
        return {"exito": False, "error": f"Error al programar recordatorio: {str(e)}"}


async def tool_consultar_recordatorios(chat_id: int):
    query = """
        SELECT id, mensaje, fecha_recordatorio
        FROM recordatorios
        WHERE enviado = FALSE AND chat_id = $1
        ORDER BY fecha_recordatorio ASC
    """
    resultados = await ejecutar_query(query, (chat_id,), fetch=True)
    if not resultados:
        return {"exito": True, "mensaje": "No hay recordatorios pendientes.", "data": []}
    data = []
    for r in resultados:
        fecha_local = utc_a_local(r["fecha_recordatorio"])
        data.append({"id_recordatorio": r["id"], "mensaje": r["mensaje"], "fecha_local": fecha_local.strftime("%Y-%m-%d %H:%M:%S")})
    return {"exito": True, "data": data}


async def tool_borrar_recordatorio(id_recordatorio: int, chat_id: int):
    await ejecutar_query("DELETE FROM recordatorios WHERE id = $1 AND chat_id = $2", (id_recordatorio, chat_id))
    return {"exito": True, "mensaje": f"🗑️ Recordatorio con ID {id_recordatorio} eliminado."}


async def tool_editar_recordatorio(
    id_recordatorio: int,
    nuevo_mensaje: str = None,
    nueva_fecha: str = None,
    chat_id: int = None,
):
    if id_recordatorio is None:
        return {"exito": False, "error": "❌ Para editar un recordatorio debes proporcionar el ID exacto."}
    updates, params = [], []
    if nuevo_mensaje:
        updates.append(f"mensaje = ${len(params)+1}")
        params.append(nuevo_mensaje)
    if nueva_fecha:
        fecha_normalizada, ambiguedad, fecha_local = interpretar_fecha(nueva_fecha)
        if not fecha_normalizada:
            if ambiguedad:
                return {"exito": False, "error": ambiguedad}
            return {"exito": False, "error": "Formato de fecha inválido."}
        fecha_utc = local_a_utc(fecha_local)
        updates.append(f"fecha_recordatorio = ${len(params)+1}")
        params.append(fecha_utc)
    if not updates:
        return {"exito": False, "error": "No se enviaron datos para actualizar."}
    params.append(id_recordatorio)
    if chat_id is not None:
        params.append(chat_id)
        set_clause = ", ".join(updates)
        query = f"UPDATE recordatorios SET {set_clause} WHERE id = ${len(params)-1} AND chat_id = ${len(params)}"
    else:
        set_clause = ", ".join(updates)
        query = f"UPDATE recordatorios SET {set_clause} WHERE id = ${len(params)}"
    await ejecutar_query(query, params)
    return {"exito": True, "mensaje": f"✏️ Recordatorio {id_recordatorio} actualizado correctamente."}


# ==================== DEFINICIÓN DE TOOLS ====================
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "tool_registrar_proyecto",
            "description": "Registra un nuevo proyecto para un cliente.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cliente": {"type": "string"},
                    "nombre_corto": {"type": "string"},
                    "descripcion": {"type": "string"},
                    "monto": {"type": "number"},
                    "telefono": {"type": "string"},
                    "direccion": {"type": "string"},
                    "notas": {"type": "string"},
                },
                "required": ["cliente", "nombre_corto", "descripcion", "monto"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_registrar_pago",
            "description": "Registra un pago o anticipo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cliente": {"type": "string"},
                    "monto": {"type": "number"},
                    "referencia": {"type": "string"},
                },
                "required": ["cliente", "monto"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_marcar_presupuesto_enviado",
            "description": "Marca un proyecto como presupuesto enviado.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cliente": {"type": "string"},
                    "nombre_corto": {"type": "string"},
                    "monto": {"type": "number"},
                    "descripcion": {"type": "string"},
                },
                "required": ["cliente"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_marcar_material_comprado",
            "description": "Marca el material de un proyecto como comprado.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cliente": {"type": "string"},
                    "nombre_corto": {"type": "string"},
                    "comprado": {"type": "boolean"},
                    "costo_material": {"type": "number"},
                },
                "required": ["cliente"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_consultar_proyectos",
            "description": "Consulta proyectos según tipo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tipo": {"type": "string", "enum": ["activos", "liquidados", "cancelados", "deudores"]},
                    "cliente": {"type": "string"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_cerrar_proyecto",
            "description": "Liquida un proyecto.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cliente": {"type": "string"},
                    "nombre_corto": {"type": "string"},
                },
                "required": ["cliente"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_cancelar_proyecto",
            "description": "Cancela un proyecto.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cliente": {"type": "string"},
                    "nombre_corto": {"type": "string"},
                },
                "required": ["cliente"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_borrar_proyecto",
            "description": "BORRA FÍSICAMENTE un proyecto. Requiere confirmación.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cliente": {"type": "string"},
                    "nombre_corto": {"type": "string"},
                    "confirmado": {"type": "boolean"},
                },
                "required": ["cliente", "confirmado"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_registrar_gasto",
            "description": "Registra un gasto general.",
            "parameters": {
                "type": "object",
                "properties": {
                    "descripcion": {"type": "string"},
                    "monto": {"type": "number"},
                },
                "required": ["descripcion", "monto"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_consultar_gastos",
            "description": "Muestra últimos gastos.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limite": {"type": "integer"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_explicar_estado",
            "description": "Explica el estado de un proyecto.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cliente": {"type": "string"},
                    "nombre_corto": {"type": "string"},
                },
                "required": ["cliente"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_editar_cliente",
            "description": "Edita información de un cliente.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cliente": {"type": "string"},
                    "telefono": {"type": "string"},
                    "direccion": {"type": "string"},
                    "notas": {"type": "string"},
                },
                "required": ["cliente"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_crear_recordatorio",
            "description": "Programa un nuevo recordatorio. Acepta fechas informales.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mensaje": {"type": "string"},
                    "fecha_recordatorio": {"type": "string"}
                },
                "required": ["mensaje", "fecha_recordatorio"]
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_consultar_recordatorios",
            "description": "Muestra recordatorios pendientes.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_borrar_recordatorio",
            "description": "Elimina un recordatorio por su ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id_recordatorio": {"type": "integer"}
                },
                "required": ["id_recordatorio"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_editar_recordatorio",
            "description": "Edita un recordatorio existente. Requiere ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id_recordatorio": {"type": "integer"},
                    "nuevo_mensaje": {"type": "string"},
                    "nueva_fecha": {"type": "string"}
                },
                "required": ["id_recordatorio"],
            },
        },
    },
]

TOOL_FUNCTIONS = {
    "tool_registrar_proyecto": tool_registrar_proyecto,
    "tool_registrar_pago": tool_registrar_pago,
    "tool_marcar_presupuesto_enviado": tool_marcar_presupuesto_enviado,
    "tool_marcar_material_comprado": tool_marcar_material_comprado,
    "tool_consultar_proyectos": tool_consultar_proyectos,
    "tool_cerrar_proyecto": tool_cerrar_proyecto,
    "tool_cancelar_proyecto": tool_cancelar_proyecto,
    "tool_borrar_proyecto": tool_borrar_proyecto,
    "tool_registrar_gasto": tool_registrar_gasto,
    "tool_consultar_gastos": tool_consultar_gastos,
    "tool_explicar_estado": tool_explicar_estado,
    "tool_editar_cliente": tool_editar_cliente,
    "tool_crear_recordatorio": tool_crear_recordatorio,
    "tool_consultar_recordatorios": tool_consultar_recordatorios,
    "tool_borrar_recordatorio": tool_borrar_recordatorio,
    "tool_editar_recordatorio": tool_editar_recordatorio,
}


# ==================== PROMPT ====================
SYSTEM_PROMPT_BASE = (
    "Eres el asistente de gestión de proyectos de un taller de aluminio. "
    "Tu tarea es registrar datos, consultar materiales y administrar pagos. "
    "No asumas información. Si algo está confuso, PREGÚNTALE al usuario. "
    "Habla de forma directa, usando 'jefe' ocasionalmente. "
    "Si el usuario pide borrar algo, pregunta confirmación primero. "
    "REGLA PARA RECORDATORIOS: "
    "Cuando el usuario pida un recordatorio, pasa la fecha y hora tal como las mencionó "
    "al parámetro fecha_recordatorio. NO intentes convertir a formato 24h tú mismo. "
    "La herramienta se encarga. Si la herramienta devuelve ambigüedad AM/PM, repregunta al usuario. "
    "Cuando el usuario aclare (ej: 'madrugada', 'de la tarde'), combina esa aclaración "
    "con la fecha original y vuelve a llamar a tool_crear_recordatorio con la frase completa. "
    "NUNCA guardes un recordatorio sin que la herramienta confirme éxito."
)


# ==================== PODA Y SANITIZACIÓN ====================
def sanitizar_historial(messages: List[Dict]) -> List[Dict]:
    """Elimina mensajes assistant con tool_calls que no tengan sus respuestas tool."""
    if not messages:
        return messages
    sanitized = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        sanitized.append(msg)
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            tool_call_ids = {tc["id"] for tc in msg["tool_calls"]}
            responses_found = set()
            j = i + 1
            while j < len(messages) and messages[j].get("role") == "tool":
                responses_found.add(messages[j].get("tool_call_id"))
                sanitized.append(messages[j])
                j += 1
            if responses_found != tool_call_ids:
                while sanitized and sanitized[-1].get("role") in ("tool", "assistant"):
                    if sanitized[-1].get("role") == "assistant" and sanitized[-1].get("tool_calls"):
                        sanitized.pop()
                        break
                    sanitized.pop()
                logger.warning("⚠️ Bloque tool_calls incompleto eliminado")
        i += 1
    return sanitized


def podar_historial(messages: List[Dict]) -> List[Dict]:
    MAX_HISTORIAL = 12
    if len(messages) <= MAX_HISTORIAL:
        return messages
    start = len(messages) - MAX_HISTORIAL
    while start > 0:
        msg = messages[start]
        if msg["role"] == "tool":
            start -= 1
            continue
        if msg["role"] == "assistant" and msg.get("tool_calls"):
            tool_call_ids = [tc["id"] for tc in msg["tool_calls"]]
            next_msgs = messages[start + 1 : start + len(tool_call_ids) + 1]
            found_ids = [m["tool_call_id"] for m in next_msgs if m["role"] == "tool"]
            if not all(tid in found_ids for tid in tool_call_ids):
                start -= 1
                continue
        break
    start = max(0, start)
    return messages[start:]


# ==================== AUDIO ====================
def transcribir_audio_buffer(buffer: io.BytesIO) -> str:
    if not groq_client:
        return ""
    buffer.seek(0)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp.write(buffer.read())
        tmp_path = tmp.name
    try:
        with open(tmp_path, "rb") as f:
            transcription = groq_client.audio.transcriptions.create(
                file=(tmp_path, f.read()),
                model="whisper-large-v3",
                language="es",
            )
        return transcription.text
    except Exception as e:
        logger.error(f"Error transcribiendo audio: {e}")
        return ""
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


# ==================== PROCESAR MENSAJE ====================
async def procesar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str):
    try:
        if not texto:
            await update.message.reply_text("No entendí el mensaje. ¿Puedes repetirlo?")
            return

        chat_id = update.effective_chat.id
        await guardar_historial(chat_id, {"role": "user", "content": texto})

        historial = await obtener_historial(chat_id, 20)
        historial_podado = podar_historial(historial)
        historial_podado = sanitizar_historial(historial_podado)

        fecha_actual = ahora_cdmx().strftime("%Y-%m-%d %H:%M")
        system_msg = {
            "role": "system",
            "content": f"La fecha y hora actual en México es: {fecha_actual}. {SYSTEM_PROMPT_BASE}",
        }
        mensajes_api = [system_msg] + historial_podado

        max_iteraciones = 5
        for _ in range(max_iteraciones):
            try:
                response = await deepseek_client.chat.completions.create(
                    model="deepseek-chat",
                    messages=mensajes_api,
                    tools=TOOLS,
                    tool_choice="auto",
                    temperature=0.2,
                    max_tokens=4000,
                )
            except Exception as e:
                error_msg = str(e)
                logger.error(f"❌ Error real de DeepSeek: {error_msg}", exc_info=True)
                if "tool_calls must be followed by tool" in error_msg.lower() or "400" in error_msg:
                    logger.warning("🧹 Historial corrupto. Limpiando...")
                    await limpiar_historial(chat_id)
                    await update.message.reply_text("⚠️ Limpié mi memoria automáticamente. Repite tu petición, jefe.")
                elif "maximum context length" in error_msg.lower() or "token" in error_msg.lower():
                    await update.message.reply_text("⚠️ Conversación muy larga. Escribe /start para reiniciar.")
                else:
                    await update.message.reply_text(f"❌ Error: {error_msg[:100]}")
                context.user_data["confirmacion_pendiente"] = None
                context.user_data["esperando_seleccion"] = None
                context.user_data["esperando_aclaracion_fecha"] = None
                return

            message = response.choices[0].message
            if not message.tool_calls:
                respuesta = message.content
                if respuesta:
                    await guardar_historial(chat_id, {"role": "assistant", "content": respuesta})
                    await update.message.reply_text(respuesta, parse_mode="Markdown")
                else:
                    await update.message.reply_text("⚠️ No entendí. ¿Puedes reformular?")
                return

            tool_calls = message.tool_calls

            for tool_call in tool_calls:
                if tool_call.function.name == "tool_editar_recordatorio":
                    args = json.loads(tool_call.function.arguments)
                    id_en_mensaje = re.search(r'\b\d+\b', texto)
                    palabras_edicion = ['edita', 'cambia', 'modifica', 'mueve', 'actualiza']
                    es_edicion = any(p in texto.lower() for p in palabras_edicion)
                    if not id_en_mensaje or not es_edicion:
                        logger.info(f"🔄 Redirigiendo edición sin ID a creación")
                        mensaje_texto = args.get('nuevo_mensaje', texto)
                        fecha_texto = args.get('nueva_fecha')
                        if fecha_texto:
                            tool_calls[0].function.name = "tool_crear_recordatorio"
                            nuevos_args = {"mensaje": mensaje_texto, "fecha_recordatorio": fecha_texto}
                            tool_calls[0].function.arguments = json.dumps(nuevos_args)
                        else:
                            await update.message.reply_text("⚠️ No entendí la fecha. Repite con fecha y hora.")
                            return

            mensaje_asistente = {
                "role": "assistant",
                "content": message.content,
                "tool_calls": [tc.model_dump() for tc in tool_calls],
            }
            await guardar_historial(chat_id, mensaje_asistente)

            for i, tool_call in enumerate(tool_calls):
                function_name = tool_call.function.name
                function_args = json.loads(tool_call.function.arguments)

                if function_name in ["tool_crear_recordatorio", "tool_consultar_recordatorios", "tool_borrar_recordatorio", "tool_editar_recordatorio"]:
                    function_args["chat_id"] = chat_id

                tool_func = TOOL_FUNCTIONS.get(function_name)
                if not tool_func:
                    result = {"error": f"Tool '{function_name}' no encontrada."}
                else:
                    try:
                        result = await tool_func(**function_args)
                    except Exception as e:
                        logger.error(f"Error ejecutando tool {function_name}: {e}")
                        result = {"error": str(e)}

                await guardar_historial(chat_id, {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(result),
                })

                necesita_salir = (
                    result.get("requiere_aclaracion_fecha") or
                    result.get("requiere_confirmacion") or
                    result.get("requiere_seleccion") or
                    (result.get("exito") is False and "requiere_confirmacion" not in result and "requiere_seleccion" not in result and "requiere_aclaracion_fecha" not in result)
                )

                if necesita_salir:
                    for remaining_tool in tool_calls[i+1:]:
                        await guardar_historial(chat_id, {
                            "role": "tool",
                            "tool_call_id": remaining_tool.id,
                            "content": json.dumps({"exito": False, "error": "Acción cancelada."})
                        })

                if result.get("requiere_aclaracion_fecha"):
                    context.user_data["esperando_aclaracion_fecha"] = {
                        "mensaje_original": result.get("mensaje_original", ""),
                        "fecha_ambigua": result.get("fecha_ambigua", ""),
                        "chat_id": chat_id,
                    }
                    await update.message.reply_text(result.get("error", "¿AM o PM?"))
                    return

                if result.get("requiere_confirmacion"):
                    context.user_data["confirmacion_pendiente"] = {
                        "tool_name": function_name,
                        "args_originales": function_args,
                        "fecha_mostrar": result.get("fecha_local"),
                        "mensaje": result.get("mensaje"),
                        "chat_id": chat_id,
                        "datos_originales": result.get("datos_originales"),
                        "id_recordatorio": result.get("id_recordatorio"),
                        "nuevo_mensaje": result.get("nuevo_mensaje"),
                        "nueva_fecha": result.get("nueva_fecha")
                    }
                    await update.message.reply_text(
                        f"{result.get('error')}\n\nResponde 'sí', 'mañana' o 'no'."
                    )
                    return

                if result.get("requiere_seleccion"):
                    opciones = result.get("opciones", [])
                    mensaje_opciones = f"{result.get('error', '')}\n\n"
                    for idx_op, opcion in enumerate(opciones, 1):
                        mensaje_opciones += f"{idx_op}. {opcion}\n"
                    mensaje_opciones += "\nResponde con el número o nombre del proyecto."
                    await update.message.reply_text(mensaje_opciones, parse_mode="Markdown")
                    context.user_data["esperando_seleccion"] = {
                        "tool_name": function_name,
                        "args_originales": function_args,
                        "opciones": opciones,
                        "cliente": function_args.get("cliente", ""),
                    }
                    return

                if result.get("exito") is False:
                    await update.message.reply_text(f"❌ {result.get('error', 'Error desconocido.')}")
                    return

            historial = await obtener_historial(chat_id, 20)
            historial_podado = podar_historial(historial)
            historial_podado = sanitizar_historial(historial_podado)
            mensajes_api = [system_msg] + historial_podado

        await update.message.reply_text("No pude procesar tu solicitud. ¿Puedes reformular?")

    except Exception as e:
        logger.error(f"❌ Error inesperado en procesar_mensaje: {e}", exc_info=True)
        try:
            await update.message.reply_text("⚠️ Ocurrió un error. Intenta de nuevo.")
        except:
            pass


# ==================== MANEJO DE ACLARACIÓN DE FECHA (AM/PM) ====================
async def manejar_aclaracion_fecha(update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str):
    """Cuando el usuario aclara AM/PM, combina con la fecha original y guarda directamente."""
    try:
        datos = context.user_data.get("esperando_aclaracion_fecha")
        if not datos:
            return False

        mensaje_original = datos.get("mensaje_original", "")
        fecha_ambigua = datos.get("fecha_ambigua", "")
        chat_id = datos.get("chat_id", update.effective_chat.id)

        texto_combinado = f"{fecha_ambigua} {texto}"
        logger.info(f"🔄 Combinando fecha: '{fecha_ambigua}' + '{texto}' = '{texto_combinado}'")

        fecha_normalizada, ambiguedad, fecha_local = interpretar_fecha(texto_combinado)

        if ambiguedad:
            await update.message.reply_text(f"⚠️ {ambiguedad}")
            return True

        if not fecha_local:
            await update.message.reply_text("⚠️ No entendí la hora. Usa formato numérico (ej: '3:30 AM').")
            context.user_data["esperando_aclaracion_fecha"] = None
            return True

        fecha_utc = local_a_utc(fecha_local)
        ahora_utc = datetime.utcnow()
        diferencia = (fecha_utc - ahora_utc).total_seconds()

        if diferencia < -120:
            fecha_local = fecha_local + timedelta(days=1)
            fecha_utc = local_a_utc(fecha_local)

        query = """
            INSERT INTO recordatorios (chat_id, mensaje, fecha_recordatorio, enviado)
            VALUES ($1, $2, $3, FALSE) RETURNING id
        """
        await ejecutar_query(query, (chat_id, mensaje_original, fecha_utc), fetch=True)
        fecha_mostrar = fecha_local.strftime("%d/%m/%Y %I:%M %p")
        mensaje_respuesta = f"✅ Recordatorio programado para el {fecha_mostrar}.\n\n📝 *{mensaje_original}*"
        await guardar_historial(chat_id, {"role": "assistant", "content": mensaje_respuesta})
        await update.message.reply_text(mensaje_respuesta, parse_mode="Markdown")

        context.user_data["esperando_aclaracion_fecha"] = None
        return True

    except Exception as e:
        logger.error(f"❌ Error en manejar_aclaracion_fecha: {e}", exc_info=True)
        await update.message.reply_text("⚠️ Error procesando la aclaración. Intenta de nuevo.")
        context.user_data["esperando_aclaracion_fecha"] = None
        return True


# ==================== MANEJO DE CONFIRMACIÓN ====================
async def manejar_confirmacion(update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str):
    try:
        confirmacion = context.user_data.get("confirmacion_pendiente")
        if not confirmacion:
            return False

        texto_lower = texto.lower()
        palabras_afirmativas = ["sí", "si", "yes", "y", "dale", "ok", "confirmo", "confirmar", "programa", "programar", "adelante"]
        es_afirmativo = any(p in texto_lower for p in palabras_afirmativas)
        es_manana = "mañana" in texto_lower or "manana" in texto_lower

        if es_afirmativo or es_manana:
            tool_name = confirmacion["tool_name"]
            chat_id = confirmacion["chat_id"]
            datos = confirmacion.get("datos_originales")

            if not datos or "fecha_utc" not in datos:
                await update.message.reply_text("⚠️ No pude entender la fecha. Repite tu petición.")
                context.user_data["confirmacion_pendiente"] = None
                return True

            if es_manana and "fecha_local" in datos:
                try:
                    fecha_local_str = datos["fecha_local"]
                    if isinstance(fecha_local_str, str):
                        fecha_local = datetime.fromisoformat(fecha_local_str)
                    else:
                        fecha_local = fecha_local_str
                    fecha_local = fecha_local + timedelta(days=1)
                    fecha_utc = local_a_utc(fecha_local)
                    datos["fecha_local"] = fecha_local.isoformat()
                    datos["fecha_utc"] = fecha_utc.isoformat()
                except Exception as e:
                    logger.error(f"Error ajustando fecha a mañana: {e}")

            if tool_name == "tool_crear_recordatorio" and datos:
                mensaje = datos["mensaje"]
                try:
                    fecha_utc_str = datos["fecha_utc"]
                    if isinstance(fecha_utc_str, str):
                        fecha_utc = datetime.fromisoformat(fecha_utc_str)
                    else:
                        fecha_utc = fecha_utc_str
                    fecha_local_str = datos["fecha_local"]
                    if isinstance(fecha_local_str, str):
                        fecha_local = datetime.fromisoformat(fecha_local_str)
                    else:
                        fecha_local = fecha_local_str
                except Exception as e:
                    logger.error(f"Error parseando fechas: {e}")
                    await update.message.reply_text("⚠️ Error con la fecha. Intenta de nuevo.")
                    context.user_data["confirmacion_pendiente"] = None
                    return True

                query = """
                    INSERT INTO recordatorios (chat_id, mensaje, fecha_recordatorio, enviado)
                    VALUES ($1, $2, $3, FALSE) RETURNING id
                """
                await ejecutar_query(query, (chat_id, mensaje, fecha_utc), fetch=True)
                fecha_mostrar = fecha_local.strftime("%d/%m/%Y %I:%M %p")
                mensaje_respuesta = f"✅ Recordatorio programado para el {fecha_mostrar}.\n\n📝 *{mensaje}*"
                await guardar_historial(chat_id, {"role": "assistant", "content": mensaje_respuesta})
                await update.message.reply_text(mensaje_respuesta, parse_mode="Markdown")
            else:
                args = confirmacion["args_originales"]
                args["chat_id"] = chat_id
                tool_func = TOOL_FUNCTIONS.get(tool_name)
                if tool_func:
                    try:
                        result = await tool_func(**args)
                        if result.get("exito"):
                            await update.message.reply_text(f"✅ {result.get('mensaje', 'Acción completada.')}")
                        else:
                            await update.message.reply_text(f"❌ {result.get('error', 'Error.')}")
                    except Exception as e:
                        await update.message.reply_text(f"❌ Error: {str(e)}")
            context.user_data["confirmacion_pendiente"] = None
            return True
        else:
            context.user_data["confirmacion_pendiente"] = None
            await update.message.reply_text("❌ Cancelado.")
            return True

    except Exception as e:
        logger.error(f"❌ Error en manejar_confirmacion: {e}", exc_info=True)
        await update.message.reply_text("⚠️ Error procesando confirmación.")
        context.user_data["confirmacion_pendiente"] = None
        return True


# ==================== MANEJO DE SELECCIÓN ====================
async def manejar_seleccion(update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str):
    try:
        seleccion_data = context.user_data.get("esperando_seleccion")
        if not seleccion_data:
            return False

        tool_name = seleccion_data["tool_name"]
        args_originales = seleccion_data["args_originales"]
        opciones = seleccion_data["opciones"]
        seleccionado = None
        texto_limpio = texto.strip().lower()

        if texto_limpio.isdigit():
            idx = int(texto_limpio) - 1
            if 0 <= idx < len(opciones):
                seleccionado = opciones[idx]
                match = re.search(r"'([^']+)'", seleccionado)
                if match:
                    args_originales["nombre_corto"] = match.group(1)
        else:
            for opcion in opciones:
                match = re.search(r"'([^']+)'", opcion)
                if match:
                    nombre_opcion = match.group(1).lower()
                    if nombre_opcion in texto_limpio or texto_limpio in nombre_opcion:
                        args_originales["nombre_corto"] = match.group(1)
                        seleccionado = opcion
                        break

        if not seleccionado:
            await update.message.reply_text(
                f"⚠️ No entendí. Las opciones son:\n\n" +
                "\n".join([f"{i+1}. {op}" for i, op in enumerate(opciones)]) +
                "\n\nResponde con el número o nombre."
            )
            return True

        context.user_data["esperando_seleccion"] = None
        tool_func = TOOL_FUNCTIONS.get(tool_name)
        if not tool_func:
            await update.message.reply_text("❌ Error: herramienta no encontrada.")
            return True

        try:
            result = await tool_func(**args_originales)
            chat_id = update.effective_chat.id
            if result.get("exito"):
                texto_resultado = f"✅ {result.get('mensaje', 'Completado.')}"
            else:
                texto_resultado = f"❌ {result.get('error', 'Error.')}"
            await guardar_historial(chat_id, {"role": "assistant", "content": texto_resultado})
            await update.message.reply_text(texto_resultado)
        except Exception as e:
            logger.error(f"Error ejecutando tool: {e}")
            await update.message.reply_text(f"❌ Error: {str(e)}")

        return True

    except Exception as e:
        logger.error(f"❌ Error en manejar_seleccion: {e}", exc_info=True)
        await update.message.reply_text("⚠️ Error procesando selección.")
        return True


# ==================== MANEJADORES ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = update.effective_chat.id
        await limpiar_historial(chat_id)
        context.user_data["esperando_seleccion"] = None
        context.user_data["confirmacion_pendiente"] = None
        context.user_data["esperando_aclaracion_fecha"] = None
        await update.message.reply_text(
            "¡Hola, jefe! 🛠️ Soy su asistente.\n\n"
            "Puedo ayudarle a:\n"
            "- Registrar clientes y proyectos\n"
            "- Registrar pagos y anticipos\n"
            "- Consultar proyectos\n"
            "- Programar recordatorios\n\n"
            "💡 Si me confundo, escribe /start para reiniciar."
        )
    except Exception as e:
        logger.error(f"❌ Error en start: {e}", exc_info=True)


async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        texto = update.message.text if update.message else ""
        texto_lower = texto.lower() if texto else ""

        # Válvula de escape
        if any(palabra in texto_lower for palabra in ["cancelar", "olvida", "reiniciar", "basta", "no importa"]):
            context.user_data["confirmacion_pendiente"] = None
            context.user_data["esperando_seleccion"] = None
            context.user_data["esperando_aclaracion_fecha"] = None
            await update.message.reply_text("✅ Operación cancelada. ¿En qué más te ayudo?")
            return

        # PRIORIDAD 1: Aclaración de fecha (AM/PM)
        if context.user_data.get("esperando_aclaracion_fecha"):
            if update.message.text:
                await manejar_aclaracion_fecha(update, context, update.message.text)
            else:
                await update.message.reply_text("⚠️ Responde 'AM', 'PM', 'mañana' o 'tarde'.")
            return

        # PRIORIDAD 2: Confirmación
        if context.user_data.get("confirmacion_pendiente"):
            if update.message.text:
                await manejar_confirmacion(update, context, update.message.text)
            else:
                await update.message.reply_text("⚠️ Responde 'sí', 'mañana' o 'no'.")
            return

        # PRIORIDAD 3: Selección
        if context.user_data.get("esperando_seleccion"):
            if update.message.text:
                await manejar_seleccion(update, context, update.message.text)
            else:
                await update.message.reply_text("⚠️ Responde con texto para seleccionar.")
            return

        # Normal: procesar mensaje
        if update.message.text:
            await procesar_mensaje(update, context, update.message.text)
        elif update.message.voice:
            if not groq_client:
                await update.message.reply_text("❌ Transcripción no configurada.")
                return
            try:
                await update.message.reply_text("🎙️ Escuchando...")
                voice_file = await update.message.voice.get_file()
                buffer = io.BytesIO()
                await voice_file.download_to_memory(buffer)
                texto = await asyncio.to_thread(transcribir_audio_buffer, buffer)
                if not texto:
                    await update.message.reply_text("❌ No entendí el audio.")
                    return
                await update.message.reply_text(f"📝 *\"{texto}\"*", parse_mode="Markdown")
                await procesar_mensaje(update, context, texto)
            except Exception as e:
                logger.error(f"Error manejando audio: {e}")
                await update.message.reply_text("❌ Error con el audio.")

    except Exception as e:
        logger.error(f"❌ Error en handler: {e}", exc_info=True)
        context.user_data["confirmacion_pendiente"] = None
        context.user_data["esperando_seleccion"] = None
        context.user_data["esperando_aclaracion_fecha"] = None
        try:
            await update.message.reply_text("⚠️ Error. Intenta de nuevo.")
        except:
            pass


# ==================== RECORDATORIOS JOB ====================
async def checar_recordatorios(context: ContextTypes.DEFAULT_TYPE):
    try:
        query = """
            SELECT id, chat_id, mensaje, fecha_recordatorio
            FROM recordatorios
            WHERE enviado = FALSE AND fecha_recordatorio <= (NOW() AT TIME ZONE 'UTC')
        """
        pendientes = await ejecutar_query(query, fetch=True)
        logger.info(f"🔍 Revisando recordatorios: {len(pendientes)} pendientes")
        for row in pendientes:
            mensaje = f"🔔 *RECORDATORIO:*\n{row['mensaje']}"
            try:
                await context.bot.send_message(chat_id=row['chat_id'], text=mensaje, parse_mode="Markdown")
                await ejecutar_query("UPDATE recordatorios SET enviado = TRUE WHERE id = $1", (row['id'],))
                logger.info(f"✅ Recordatorio {row['id']} enviado")
            except Exception as e:
                logger.error(f"❌ Error enviando recordatorio {row['id']}: {e}")
    except Exception as e:
        logger.error(f"❌ Error en checar_recordatorios: {e}")


# ==================== INICIO ====================
async def post_init(app):
    await init_db_pool()
    await crear_tablas()
    logger.info("✅ Base de datos inicializada.")


def main():
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handler))
    app.add_handler(MessageHandler(filters.VOICE, handler))

    job_queue = app.job_queue
    if job_queue:
        job_queue.run_repeating(checar_recordatorios, interval=60, first=10)
        logger.info("✅ JobQueue para recordatorios iniciado.")
    else:
        logger.warning("⚠️ JobQueue no disponible.")

    logger.info("🤖 Bot iniciado con todas las correcciones.")
    app.run_polling()


if __name__ == "__main__":
    main()
