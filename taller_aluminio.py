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
    PicklePersistence,
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

deepseek_client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com", timeout=60.0)
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
            DATABASE_URL,
            min_size=1,
            max_size=20,
            command_timeout=30,
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

# ==================== CAMBIO 3: Orden cronológico estricto ====================
async def obtener_historial(chat_id: int, limite: int = 30) -> List[Dict]:
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
        "error": f"{cliente} tiene {len(proyectos)} proyectos activos. Pregunta cuál y vuelve a llamar con 'nombre_corto'."
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
    pid, nc, desc, total, pagado, estado, mat_comp, costo_mat, presup, cliente_nombre = (
        proyecto["id"],
        proyecto["nombre_corto"],
        proyecto["descripcion"],
        proyecto["monto_total"],
        proyecto["monto_pagado"],
        proyecto["estado"],
        proyecto["material_comprado"],
        proyecto["costo_material"],
        proyecto["presupuesto_enviado"],
        proyecto["cliente"],
    )
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
    pid, nc, desc, total, pagado, estado, mat_comp, costo_mat, presup, cliente_nombre = (
        proyecto["id"],
        proyecto["nombre_corto"],
        proyecto["descripcion"],
        proyecto["monto_total"],
        proyecto["monto_pagado"],
        proyecto["estado"],
        proyecto["material_comprado"],
        proyecto["costo_material"],
        proyecto["presupuesto_enviado"],
        proyecto["cliente"],
    )
    updates = ["presupuesto_enviado = TRUE", "fecha_presupuesto = CURRENT_TIMESTAMP", "estado = 'Presupuesto enviado'"]
    params = []
    if monto is not None and monto > 0:
        updates.append(f"monto_total = ${len(params)+1}")
        params.append(monto)
    if descripcion:
        updates.append(f"descripcion = COALESCE(${len(params)+1}, descripcion)")
        params.append(descripcion)
    params.append(pid)
    await ejecutar_query(f"UPDATE proyectos SET {', '.join(updates)} WHERE id = ${len(params)}", params)
    return {"exito": True, "mensaje": f"Presupuesto marcado como enviado para '{nc}' de {cliente}."}

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
                   p.material_comprado, p.presupuesto_enviado, c.telefono, c.direccion
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
                   p.material_comprado, p.presupuesto_enviado, c.telefono, c.direccion
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
    pid, nc, desc, total, pagado, estado, mat_comp, costo_mat, presup, cliente_nombre = (
        proyecto["id"],
        proyecto["nombre_corto"],
        proyecto["descripcion"],
        proyecto["monto_total"],
        proyecto["monto_pagado"],
        proyecto["estado"],
        proyecto["material_comprado"],
        proyecto["costo_material"],
        proyecto["presupuesto_enviado"],
        proyecto["cliente"],
    )
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
    proyectos = []
    for row in proyectos_raw:
        proyectos.append({
            "id": row["id"],
            "nombre_corto": row["nombre_corto"],
            "descripcion": row["descripcion"],
            "monto_total": row["monto_total"],
            "monto_pagado": row["monto_pagado"],
            "estado": row["estado"],
            "material_comprado": row["material_comprado"],
            "costo_material": row["costo_material"],
            "presupuesto_enviado": row["presupuesto_enviado"],
            "cliente": row["cliente"],
        })
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
    proyectos = []
    for row in proyectos_raw:
        proyectos.append({
            "id": row["id"],
            "nombre_corto": row["nombre_corto"],
            "descripcion": row["descripcion"],
            "monto_total": row["monto_total"],
            "monto_pagado": row["monto_pagado"],
            "estado": row["estado"],
            "material_comprado": row["material_comprado"],
            "costo_material": row["costo_material"],
            "presupuesto_enviado": row["presupuesto_enviado"],
            "cliente": row["cliente"],
        })
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
    pid, nc, desc, total, pagado, estado, mat_comp, costo_mat, presup, cliente_nombre = (
        proyecto["id"],
        proyecto["nombre_corto"],
        proyecto["descripcion"],
        proyecto["monto_total"],
        proyecto["monto_pagado"],
        proyecto["estado"],
        proyecto["material_comprado"],
        proyecto["costo_material"],
        proyecto["presupuesto_enviado"],
        proyecto["cliente"],
    )
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

# ==================== RECORDATORIOS ====================
def _normalizar_texto_hora(texto: str) -> str:
    """Normaliza expresiones comunes de hora en español."""
    texto = texto.lower().strip()
    reemplazos = {
        "a.m.": "am", "p.m.": "pm", "a. m.": "am", "p. m.": "pm",
        "a m": "am", "p m": "pm",
        "mediodia": "mediodía", "medianoche": "00:00",
    }
    for a, b in reemplazos.items():
        texto = texto.replace(a, b)
    return texto


def interpretar_fecha(fecha_texto: str) -> Tuple[Optional[str], Optional[str], Optional[datetime]]:
    """Interpreta fechas/horas informales en español.

    Importante: si el usuario escribió AM/PM o una expresión inequívoca
    (madrugada, mañana, tarde, noche), nunca vuelve a preguntar AM/PM.
    """
    original = fecha_texto or ""
    texto = _normalizar_texto_hora(original)
    hoy = ahora_cdmx()
    fecha_actual = hoy.strftime("%Y-%m-%d")

    # Fecha relativa.
    if re.search(r'\bpasado mañana\b', texto):
        fecha_base = hoy + timedelta(days=2)
    elif re.search(r'\bmañana\b|\bmanana\b', texto):
        fecha_base = hoy + timedelta(days=1)
    else:
        fecha_base = hoy

    # Fecha YYYY-MM-DD, DD/MM/YYYY o DD-MM-YYYY.
    fecha_str = fecha_actual
    m_iso = re.search(r'\b(20\d{2})-(\d{1,2})-(\d{1,2})\b', texto)
    m_lat = re.search(r'\b(\d{1,2})[/-](\d{1,2})[/-](20\d{2})\b', texto)
    try:
        if m_iso:
            y, mo, d = map(int, m_iso.groups())
            fecha_base = hoy.replace(year=y, month=mo, day=d)
            fecha_str = fecha_base.strftime("%Y-%m-%d")
        elif m_lat:
            d, mo, y = map(int, m_lat.groups())
            fecha_base = hoy.replace(year=y, month=mo, day=d)
            fecha_str = fecha_base.strftime("%Y-%m-%d")
        else:
            fecha_str = fecha_base.strftime("%Y-%m-%d")
    except ValueError:
        return None, "La fecha indicada no es válida. Usa, por ejemplo, 30/08/2026.", None

    # Detectar contexto horario ANTES de extraer números.
    tiene_am = bool(re.search(r'\b(?:am|a\.m\.)\b', texto))
    tiene_pm = bool(re.search(r'\b(?:pm|p\.m\.)\b', texto))
    es_madrugada = bool(re.search(r'\b(?:madrugada)\b', texto))
    es_manana = bool(re.search(r'\b(?:mañana|manana)\b', texto))
    es_tarde = bool(re.search(r'\b(?:tarde)\b', texto))
    es_noche = bool(re.search(r'\b(?:noche)\b', texto))
    contexto_inequivoco = tiene_am or tiene_pm or es_madrugada or es_manana or es_tarde or es_noche

    # Extraer hora/minuto. Soporta 3:30, 3.30, 3 30, 3 y 30, 3 con 30,
    # "3 y media" y "3:30 AM".
    hora = minuto = None
    patrones = [
        r'\b(\d{1,2})\s*[:.,]\s*(\d{1,2})\b',
        r'\b(\d{1,2})\s+(?:y|con)\s+(\d{1,2})\b',
        r'\b(\d{1,2})\s+(?:y\s+)?media\b',
    ]
    for patron in patrones:
        m = re.search(patron, texto)
        if m:
            hora = int(m.group(1))
            minuto = 30 if len(m.groups()) == 1 else int(m.group(2))
            break

    if hora is None:
        # Caso "3 30" sin separador.
        m = re.search(r'\b(\d{1,2})\s+(\d{2})\b', texto)
        if m:
            hora, minuto = int(m.group(1)), int(m.group(2))
        else:
            # Caso "a las 3".
            m = re.search(r'\b(?:a\s+las|a\s+la|las|la)\s+(\d{1,2})\b', texto)
            if m:
                hora, minuto = int(m.group(1)), 0
            else:
                # Último recurso: primer número de 1-2 dígitos que parezca hora.
                nums = re.findall(r'\b\d{1,2}\b', texto)
                if nums:
                    candidato = int(nums[0])
                    if 0 <= candidato <= 23:
                        hora, minuto = candidato, 0

    if hora is None:
        return None, "No encontré una hora. Por favor, dime la hora, por ejemplo: '3:30 AM'.", None
    if minuto is None:
        minuto = 0
    if hora > 23 or minuto > 59:
        return None, "La hora indicada no es válida. Usa una hora entre 00:00 y 23:59.", None

    # PRIORIDAD ABSOLUTA: AM/PM explícito.
    if tiene_am:
        if hora == 12:
            hora = 0
    elif tiene_pm:
        if hora < 12:
            hora += 12
    elif es_madrugada:
        if hora == 12:
            hora = 0
        elif hora >= 6:
            # 6+ ya no es madrugada; no forzamos una conversión absurda.
            return None, "Para esa hora necesito saber si es de la mañana o de la tarde.", None
    elif es_manana:
        if hora == 12:
            hora = 0
    elif es_tarde or es_noche:
        if hora < 12:
            hora += 12
    else:
        # Solo preguntamos cuando realmente hay ambigüedad.
        # Las horas 6-11 se interpretan como mañana; 12 se interpreta como mediodía.
        # 0-5 se consideran madrugada por defecto, porque el usuario ya dio una hora válida.
        if 6 <= hora <= 11:
            pass
        elif hora == 12:
            pass
        elif 0 <= hora <= 5:
            pass

    fecha_normalizada = f"{fecha_str} {hora:02d}:{minuto:02d}:00"
    try:
        fecha_local = datetime.strptime(fecha_normalizada, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None, "Formato de fecha inválido.", None
    return fecha_normalizada, None, fecha_local


async def tool_crear_recordatorio(mensaje: str, fecha_recordatorio: str, chat_id: int):
    """Prepara un recordatorio y SIEMPRE exige confirmación antes de insertarlo."""
    try:
        fecha_normalizada, ambiguedad, fecha_local = interpretar_fecha(fecha_recordatorio)
        if not fecha_normalizada or not fecha_local:
            return {"exito": False, "error": ambiguedad or f"⚠️ No pude interpretar la fecha: '{fecha_recordatorio}'."}

        fecha_utc = local_a_utc(fecha_local)
        ahora_utc = datetime.utcnow()
        diferencia = (fecha_utc - ahora_utc).total_seconds()
        fecha_mostrar = fecha_local.strftime("%d/%m/%Y %I:%M %p")

        if diferencia <= 0:
            fecha_manana = fecha_local + timedelta(days=1)
            fecha_manana_str = fecha_manana.strftime("%d/%m/%Y %I:%M %p")
            aviso = (
                f"⚠️ La fecha programada ({fecha_mostrar}) ya pasó. "
                f"¿Quieres programarla para mañana ({fecha_manana_str}) o mantener la fecha actual?"
            )
        elif diferencia <= 180:
            hora_actual = ahora_cdmx().strftime("%I:%M %p")
            aviso = (
                f"⚠️ La fecha programada ({fecha_mostrar}) es muy cercana. "
                f"La hora actual es {hora_actual}. ¿Quieres programarla igualmente?"
            )
        else:
            aviso = f"Confirma el recordatorio para el {fecha_mostrar}."

        return {
            "exito": False,
            "requiere_confirmacion": True,
            "fecha_local": fecha_mostrar,
            "mensaje": mensaje,
            "error": aviso,
            "datos_originales": {
                "mensaje": mensaje,
                "fecha_local": fecha_local.isoformat(),
                "fecha_utc": fecha_utc.isoformat(),
            },
        }
    except Exception as e:
        logger.error(f"Error en tool_crear_recordatorio: {e}", exc_info=True)
        return {"exito": False, "error": f"Error al preparar el recordatorio: {str(e)}"}

async def tool_consultar_recordatorios(chat_id: int):
    query = """
        SELECT id, mensaje, fecha_recordatorio
        FROM recordatorios
        WHERE enviado = FALSE AND chat_id = $1
        ORDER BY fecha_recordatorio ASC
    """
    resultados = await ejecutar_query(query, (chat_id,), fetch=True)
    if not resultados:
        return {"exito": True, "mensaje": "No hay recordatorios pendientes en este momento.", "data": []}
    data = []
    for r in resultados:
        fecha_local = utc_a_local(r["fecha_recordatorio"])
        data.append({"id_recordatorio": r["id"], "mensaje": r["mensaje"], "fecha_local": fecha_local.strftime("%Y-%m-%d %H:%M:%S")})
    return {"exito": True, "data": data}

async def tool_borrar_recordatorio(id_recordatorio: int, chat_id: int):
    result = await ejecutar_query(
        "DELETE FROM recordatorios WHERE id = $1 AND chat_id = $2",
        (id_recordatorio, chat_id),
    )
    if not result or result.endswith(" 0"):
        return {"exito": False, "error": f"No encontré un recordatorio pendiente con ID {id_recordatorio}."}
    return {"exito": True, "mensaje": f"🗑️ Recordatorio con ID {id_recordatorio} eliminado."}

async def tool_editar_recordatorio(
    id_recordatorio: int,
    nuevo_mensaje: str = None,
    nueva_fecha: str = None,
    chat_id: int = None,
):
    if id_recordatorio is None:
        return {"exito": False, "error": "❌ Para editar un recordatorio debes proporcionar el ID exacto."}

    existe = await ejecutar_query(
        "SELECT id, mensaje, fecha_recordatorio FROM recordatorios WHERE id = $1 AND chat_id = $2 AND enviado = FALSE",
        (id_recordatorio, chat_id), fetch=True
    ) if chat_id is not None else await ejecutar_query(
        "SELECT id, mensaje, fecha_recordatorio FROM recordatorios WHERE id = $1 AND enviado = FALSE",
        (id_recordatorio,), fetch=True
    )
    if not existe:
        return {"exito": False, "error": f"No encontré un recordatorio pendiente con ID {id_recordatorio}."}

    updates, params = [], []
    if nuevo_mensaje is not None and str(nuevo_mensaje).strip():
        updates.append(f"mensaje = ${len(params)+1}")
        params.append(str(nuevo_mensaje).strip())

    fecha_local = None
    if nueva_fecha:
        fecha_normalizada, ambiguedad, fecha_local = interpretar_fecha(nueva_fecha)
        if not fecha_normalizada or not fecha_local:
            return {"exito": False, "error": ambiguedad or "Formato de fecha inválido."}
        fecha_utc = local_a_utc(fecha_local)
        ahora_utc = datetime.utcnow()
        diferencia = (fecha_utc - ahora_utc).total_seconds()
        if diferencia <= 180:
            fecha_mostrar = fecha_local.strftime("%d/%m/%Y %I:%M %p")
            hora_actual = ahora_cdmx().strftime("%I:%M %p")
            return {
                "exito": False,
                "requiere_confirmacion": True,
                "fecha_local": fecha_mostrar,
                "mensaje": nuevo_mensaje or existe[0]["mensaje"],
                "error": f"⚠️ La nueva fecha ({fecha_mostrar}) es muy cercana o ya pasó. La hora actual es {hora_actual}. ¿Quieres actualizarlo igualmente?",
                "id_recordatorio": id_recordatorio,
                "nuevo_mensaje": nuevo_mensaje,
                "nueva_fecha": nueva_fecha,
                "datos_originales": {
                    "mensaje": nuevo_mensaje or existe[0]["mensaje"],
                    "fecha_local": fecha_local.isoformat(),
                    "fecha_utc": fecha_utc.isoformat(),
                },
            }
        updates.append(f"fecha_recordatorio = ${len(params)+1}")
        params.append(fecha_utc)

    if not updates:
        return {"exito": False, "error": "No se enviaron datos para actualizar."}

    params.append(id_recordatorio)
    where = f"id = ${len(params)}"
    if chat_id is not None:
        params.append(chat_id)
        where += f" AND chat_id = ${len(params)}"
    query = f"UPDATE recordatorios SET {', '.join(updates)} WHERE {where}"
    await ejecutar_query(query, params)
    return {"exito": True, "mensaje": f"✏️ Recordatorio {id_recordatorio} actualizado correctamente."}

# ==================== DEFINICIÓN DE TOOLS (15 tools) ====================
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "tool_registrar_proyecto",
            "description": "Registra un nuevo proyecto para un cliente. Si falta información, pregunta antes de llamar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cliente": {"type": "string", "description": "Nombre del cliente"},
                    "nombre_corto": {"type": "string", "description": "Nombre corto del proyecto"},
                    "descripcion": {"type": "string", "description": "Descripción del trabajo"},
                    "monto": {"type": "number", "description": "Presupuesto total del proyecto"},
                    "telefono": {"type": "string", "description": "Teléfono del cliente (opcional)"},
                    "direccion": {"type": "string", "description": "Dirección del cliente (opcional)"},
                    "notas": {"type": "string", "description": "Notas adicionales (opcional)"},
                },
                "required": ["cliente", "nombre_corto", "descripcion", "monto"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_registrar_pago",
            "description": "Registra un pago o anticipo de un cliente. Usa el campo 'referencia' para especificar el proyecto si el cliente tiene varios.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cliente": {"type": "string", "description": "Nombre del cliente"},
                    "monto": {"type": "number", "description": "Monto del pago (debe ser > 0)"},
                    "referencia": {"type": "string", "description": "Nombre corto del proyecto (opcional, para desambiguar si hay varios)"},
                },
                "required": ["cliente", "monto"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_marcar_presupuesto_enviado",
            "description": "Marca un proyecto como 'Presupuesto enviado'. Usa 'nombre_corto' para desambiguar si el cliente tiene varios proyectos.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cliente": {"type": "string", "description": "Nombre del cliente"},
                    "nombre_corto": {"type": "string", "description": "Nombre corto del proyecto (opcional si solo tiene uno)"},
                    "monto": {"type": "number", "description": "Nuevo monto total (opcional)"},
                    "descripcion": {"type": "string", "description": "Nueva descripción (opcional)"},
                },
                "required": ["cliente"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_consultar_proyectos",
            "description": "Consulta proyectos según tipo. Devuelve un máximo de 5 resultados.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tipo": {"type": "string", "enum": ["activos", "liquidados", "cancelados", "deudores"], "description": "Tipo de consulta"},
                    "cliente": {"type": "string", "description": "Nombre del cliente (opcional) para filtrar"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_cerrar_proyecto",
            "description": "Liquida un proyecto. Usa 'nombre_corto' para desambiguar si hay varios.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cliente": {"type": "string", "description": "Nombre del cliente"},
                    "nombre_corto": {"type": "string", "description": "Nombre corto del proyecto (opcional si solo tiene uno)"},
                },
                "required": ["cliente"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_cancelar_proyecto",
            "description": "Cancela un proyecto. Usa 'nombre_corto' para desambiguar si hay varios.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cliente": {"type": "string", "description": "Nombre del cliente"},
                    "nombre_corto": {"type": "string", "description": "Nombre corto del proyecto (opcional si solo tiene uno)"},
                },
                "required": ["cliente"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_borrar_proyecto",
            "description": "BORRA FÍSICAMENTE un proyecto. Usa 'nombre_corto' para desambiguar si hay varios. Requiere confirmación explícita.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cliente": {"type": "string", "description": "Nombre del cliente"},
                    "nombre_corto": {"type": "string", "description": "Nombre corto del proyecto (opcional si solo tiene uno)"},
                    "confirmado": {"type": "boolean", "description": "Debe ser true solo si el usuario confirmó explícitamente con 'sí'"},
                },
                "required": ["cliente", "confirmado"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_registrar_gasto",
            "description": "Registra un gasto general (no asociado a proyecto).",
            "parameters": {
                "type": "object",
                "properties": {
                    "descripcion": {"type": "string", "description": "Descripción del gasto"},
                    "monto": {"type": "number", "description": "Monto del gasto (debe ser > 0)"},
                },
                "required": ["descripcion", "monto"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_consultar_gastos",
            "description": "Muestra los últimos gastos registrados.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limite": {"type": "integer", "description": "Número de gastos a mostrar (por defecto 10)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_explicar_estado",
            "description": "Explica por qué un proyecto está en su estado actual. Usa 'nombre_corto' para desambiguar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cliente": {"type": "string", "description": "Nombre del cliente"},
                    "nombre_corto": {"type": "string", "description": "Nombre corto del proyecto (opcional si solo tiene uno)"},
                },
                "required": ["cliente"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_editar_cliente",
            "description": "Edita o agrega información de un cliente existente.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cliente": {"type": "string", "description": "Nombre del cliente a editar"},
                    "telefono": {"type": "string", "description": "Nuevo número de teléfono (opcional)"},
                    "direccion": {"type": "string", "description": "Nueva dirección (opcional)"},
                    "notas": {"type": "string", "description": "Nuevas notas (opcional)"},
                },
                "required": ["cliente"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_crear_recordatorio",
            "description": "PROGRAMA UN NUEVO RECORDATORIO. Interpreta fechas informales (ej. 'a las 2 con 35 de la mañana') y pregunta confirmación si hay ambigüedad.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mensaje": {"type": "string", "description": "Texto del recordatorio"},
                    "fecha_recordatorio": {"type": "string", "description": "Fecha y hora en formato informal (ej. 'hoy a las 2 con 35 de la mañana')"}
                },
                "required": ["mensaje", "fecha_recordatorio"]
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_consultar_recordatorios",
            "description": "Muestra la lista de recordatorios pendientes y sus IDs.",
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
            "description": "Elimina un recordatorio programado utilizando su ID exacto.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id_recordatorio": {"type": "integer", "description": "El ID numérico del recordatorio a borrar"}
                },
                "required": ["id_recordatorio"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_editar_recordatorio",
            "description": "SOLO PARA EDITAR UN RECORDATORIO EXISTENTE. Requiere que el usuario proporcione el ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id_recordatorio": {"type": "integer", "description": "El ID numérico del recordatorio a editar (OBLIGATORIO)"},
                    "nuevo_mensaje": {"type": "string", "description": "El nuevo texto del recordatorio (opcional)"},
                    "nueva_fecha": {"type": "string", "description": "La nueva fecha en formato informal (opcional)"}
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
    "Tu tarea es ayudar a registrar datos, consultar materiales y administrar pagos. "
    "No asumas información. Si el usuario escribe algo confuso, con errores tipográficos o incompleto, "
    "PREGÚNTALE para aclarar antes de ejecutar cualquier acción. "
    "Si no entiendes la fecha, hora o el mensaje, pide que lo repita con claridad. "
    "NUNCA te quedes en silencio ni ejecutes sin confirmación si falta información. "
    "Habla de forma directa y clara, usando 'jefe' o 'patrón' ocasionalmente. "
    "Cuando muestres listas, preséntalas de manera ordenada, con emojis. "
    "Si el usuario pide borrar algo, siempre pregunta confirmación primero. "
    "REGLA PARA RECORDATORIOS: "
    "Cuando el usuario pida un recordatorio, interpreta la fecha y hora que menciona "
    "(ej: 'a las 2 con 35 de la mañana') y llama a tool_crear_recordatorio directamente. "
    "La herramienta ya se encarga de pedir confirmación si hay ambigüedad de AM/PM o si "
    "la hora ya pasó — no le preguntes tú también en lenguaje natural antes de llamarla, "
    "para no duplicar la pregunta y hacer esperar al jefe dos veces por lo mismo."
)

# ==================== PODA ====================
def podar_historial(messages: List[Dict]) -> List[Dict]:
    """Devuelve un historial seguro para la API: nunca deja tool_calls huérfanos."""
    MAX_HISTORIAL = 12
    if not messages:
        return []

    salida = []
    i = 0
    while i < len(messages):
        m = messages[i]
        if not isinstance(m, dict) or m.get("role") not in {"user", "assistant", "tool"}:
            i += 1
            continue
        if m.get("role") == "assistant" and m.get("tool_calls"):
            ids = [tc.get("id") for tc in m.get("tool_calls", []) if tc.get("id")]
            tools = []
            j = i + 1
            while j < len(messages) and messages[j].get("role") == "tool":
                if messages[j].get("tool_call_id") in ids:
                    tools.append(messages[j])
                j += 1
            found = {x.get("tool_call_id") for x in tools}
            if not ids or not all(x in found for x in ids):
                # No mandamos un assistant con tool_calls incompletos.
                i = j
                continue
            salida.append(m)
            salida.extend(tools)
            i = j
            continue
        if m.get("role") == "tool":
            i += 1
            continue
        salida.append(m)
        i += 1

    # Conserva los últimos mensajes, pero sin cortar un grupo assistant+tools.
    if len(salida) <= MAX_HISTORIAL:
        return salida
    salida = salida[-MAX_HISTORIAL:]
    while salida and salida[0].get("role") == "tool":
        salida.pop(0)
    if salida and salida[0].get("role") == "assistant" and salida[0].get("tool_calls"):
        ids = {tc.get("id") for tc in salida[0].get("tool_calls", [])}
        siguiente = [m.get("tool_call_id") for m in salida[1:] if m.get("role") == "tool"]
        if not ids.issubset(set(siguiente)):
            salida.pop(0)
    return salida

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

# ==================== PROCESAR MENSAJE (CON CAMBIOS 1 Y 2) ====================
async def procesar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str):
    try:
        if not texto:
            await update.message.reply_text("No entendí el mensaje. ¿Puedes repetirlo?")
            return

        chat_id = update.effective_chat.id
        await guardar_historial(chat_id, {"role": "user", "content": texto})

        # CAMBIO 2: obtener_historial con límite 20 (antes era 6)
        historial = await obtener_historial(chat_id, 30)
        historial_podado = podar_historial(historial)

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
                if "maximum context length" in error_msg.lower() or "token" in error_msg.lower():
                    await update.message.reply_text("⚠️ La conversación es muy larga. Por favor, escribe /start para reiniciar y limpiar la memoria.")
                elif "tool_calls must be followed by tool" in error_msg.lower():
                    logger.warning("🧹 Historial corrupto detectado. Limpiando automáticamente...")
                    await limpiar_historial(chat_id)
                    await update.message.reply_text(
                        "⚠️ Detecté un error en mi memoria y la limpié automáticamente. "
                        "Por favor, repite tu última petición, jefe."
                    )
                else:
                    await update.message.reply_text(f"❌ Error de conexión con la IA: {error_msg[:100]}")
                context.user_data["confirmacion_pendiente"] = None
                context.user_data["esperando_seleccion"] = None
                return

            message = response.choices[0].message
            if not message.tool_calls:
                respuesta = message.content
                if respuesta:
                    await guardar_historial(chat_id, {"role": "assistant", "content": respuesta})
                    await update.message.reply_text(respuesta, parse_mode="Markdown")
                else:
                    await update.message.reply_text("⚠️ No entendí tu solicitud. ¿Puedes reformularla con más claridad?")
                return

            tool_calls = message.tool_calls

            # Intercepción: editar sin ID -> crear
            for tool_call in tool_calls:
                if tool_call.function.name == "tool_editar_recordatorio":
                    args = json.loads(tool_call.function.arguments)
                    id_en_mensaje = re.search(r'\b\d+\b', texto)
                    palabras_edicion = ['edita', 'cambia', 'modifica', 'mueve', 'actualiza']
                    es_edicion_explicita = any(p in texto.lower() for p in palabras_edicion)
                    if not id_en_mensaje or not es_edicion_explicita:
                        logger.info(f"🔄 Redirigiendo edición sin ID a creación: {texto}")
                        mensaje_texto = args.get('nuevo_mensaje', texto)
                        fecha_texto = args.get('nueva_fecha')
                        if fecha_texto:
                            tool_calls[0].function.name = "tool_crear_recordatorio"
                            nuevos_args = {"mensaje": mensaje_texto, "fecha_recordatorio": fecha_texto}
                            tool_calls[0].function.arguments = json.dumps(nuevos_args)
                        else:
                            await update.message.reply_text("⚠️ No pude entender la fecha. Por favor, repite con la fecha y hora.")
                            return

            mensaje_asistente = {
                "role": "assistant",
                "content": message.content,
                "tool_calls": [tc.model_dump() for tc in tool_calls],
            }
            await guardar_historial(chat_id, mensaje_asistente)

            # CAMBIO 1: Enumerar tool_calls para rellenar huérfanos
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

                # CAMBIO 1: Rellenar llamadas restantes para evitar huérfanos
                necesita_salir = (
                    (result.get("exito") is False and "requiere_confirmacion" not in result and "requiere_seleccion" not in result) or
                    result.get("requiere_confirmacion") or
                    result.get("requiere_seleccion")
                )

                if necesita_salir:
                    # Rellenar las llamadas restantes para que no queden huérfanas en la API
                    for remaining_tool in tool_calls[i+1:]:
                        await guardar_historial(chat_id, {
                            "role": "tool",
                            "tool_call_id": remaining_tool.id,
                            "content": json.dumps({"exito": False, "error": "Acción cancelada. Se requiere interacción previa."})
                        })

                if result.get("exito") is False and "requiere_confirmacion" not in result and "requiere_seleccion" not in result:
                    await update.message.reply_text(f"❌ {result.get('error', 'Error desconocido.')}")
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
                        f"{result.get('error')}\n\n"
                        "Responde 'sí' para programarlo igual, 'mañana' para programarlo para mañana, o 'no' para cancelar."
                    )
                    return

                if result.get("requiere_seleccion"):
                    opciones = result.get("opciones", [])
                    mensaje_opciones = f"{result.get('error', '')}\n\n"
                    for idx_opcion, opcion in enumerate(opciones, 1):
                        mensaje_opciones += f"{idx_opcion}. {opcion}\n"
                    mensaje_opciones += "\nResponde con el nombre exacto del proyecto o el número."
                    await update.message.reply_text(mensaje_opciones, parse_mode="Markdown")
                    context.user_data["esperando_seleccion"] = {
                        "tool_name": function_name,
                        "args_originales": function_args,
                        "opciones": opciones,
                        "cliente": function_args.get("cliente", ""),
                    }
                    return

            # CAMBIO 2: obtener_historial con límite 20 (antes era 6)
            historial = await obtener_historial(chat_id, 30)
            historial_podado = podar_historial(historial)
            mensajes_api = [system_msg] + historial_podado

        await update.message.reply_text("Lo siento, no pude procesar tu solicitud. ¿Puedes reformularla con más claridad?")

    except Exception as e:
        logger.error(f"❌ Error inesperado en procesar_mensaje: {e}", exc_info=True)
        try:
            await update.message.reply_text("⚠️ Ocurrió un error inesperado. Por favor, intenta de nuevo.")
        except:
            pass

# ==================== MANEJO DE CONFIRMACIÓN ====================
async def manejar_confirmacion(update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str):
    """Procesa una confirmación sin volver a reinterpretar innecesariamente la fecha."""
    try:
        confirmacion = context.user_data.get("confirmacion_pendiente")
        if not confirmacion:
            return False

        t = (texto or "").strip().lower()
        t_norm = re.sub(r'[^a-záéíóúñü0-9 ]+', ' ', t)
        t_norm = re.sub(r'\s+', ' ', t_norm).strip()

        afirmativas = {
            "sí", "si", "s", "yes", "y", "dale", "ok", "okay", "confirmo",
            "confirmar", "confirmado", "adelante", "programa", "programar"
        }
        negativas = {"no", "n", "cancelar", "cancela", "cancelo", "olvida"}
        es_afirmativo = t_norm in afirmativas or t_norm.startswith("si ") or t_norm.startswith("sí ")
        es_negativo = t_norm in negativas
        es_manana = t_norm in {"mañana", "manana", "para mañana", "para manana"}

        if es_negativo:
            context.user_data["confirmacion_pendiente"] = None
            await update.message.reply_text("❌ Recordatorio cancelado.")
            return True

        tool_name = confirmacion.get("tool_name")
        chat_id = confirmacion.get("chat_id", update.effective_chat.id)
        mensaje_original = confirmacion.get("mensaje") or confirmacion.get("args_originales", {}).get("mensaje") or "Recordatorio"

        # Caso especial: confirmación normal. NO reinterpretar la hora.
        if es_afirmativo:
            datos = confirmacion.get("datos_originales") or {}
            fecha_local_iso = datos.get("fecha_local")
            if fecha_local_iso:
                fecha_local = datetime.fromisoformat(fecha_local_iso)
            else:
                _, _, fecha_local = interpretar_fecha(confirmacion.get("args_originales", {}).get("fecha_recordatorio", ""))
            if not fecha_local:
                await update.message.reply_text("⚠️ Perdí la fecha del recordatorio. Vuelve a indicarme la hora.")
                context.user_data["confirmacion_pendiente"] = None
                return True

            if tool_name == "tool_editar_recordatorio":
                id_recordatorio = confirmacion.get("id_recordatorio")
                nueva_fecha = local_a_utc(fecha_local)
                result = await tool_editar_recordatorio(
                    id_recordatorio=id_recordatorio,
                    nuevo_mensaje=confirmacion.get("nuevo_mensaje"),
                    nueva_fecha=None,
                    chat_id=chat_id,
                )
                if result.get("exito"):
                    await ejecutar_query(
                        "UPDATE recordatorios SET fecha_recordatorio = $1 WHERE id = $2 AND chat_id = $3",
                        (nueva_fecha, id_recordatorio, chat_id),
                    )
                else:
                    await update.message.reply_text(f"❌ {result.get('error', 'No pude actualizar el recordatorio.')}")
                    context.user_data["confirmacion_pendiente"] = None
                    return True
            else:
                fecha_utc = local_a_utc(fecha_local)
                result = await ejecutar_query(
                    "INSERT INTO recordatorios (chat_id, mensaje, fecha_recordatorio, enviado) VALUES ($1, $2, $3, FALSE) RETURNING id",
                    (chat_id, mensaje_original, fecha_utc), fetch=True
                )
                nuevo_id = result[0]["id"]

            fecha_mostrar = fecha_local.strftime("%d/%m/%Y %I:%M %p")
            respuesta = f"✅ Recordatorio programado para el {fecha_mostrar}.\n\n📝 *{mensaje_original}*"
            await guardar_historial(chat_id, {"role": "assistant", "content": respuesta})
            await update.message.reply_text(respuesta, parse_mode="Markdown")
            context.user_data["confirmacion_pendiente"] = None
            return True

        # Si dice "mañana", conservamos la misma hora y solo cambiamos el día.
        if es_manana:
            datos = confirmacion.get("datos_originales") or {}
            fecha_local_iso = datos.get("fecha_local")
            if fecha_local_iso:
                fecha_local = datetime.fromisoformat(fecha_local_iso) + timedelta(days=1)
            else:
                _, _, fecha_local = interpretar_fecha(confirmacion.get("args_originales", {}).get("fecha_recordatorio", ""))
                if fecha_local:
                    fecha_local += timedelta(days=1)
            if not fecha_local:
                await update.message.reply_text("⚠️ No pude recuperar la hora original. Vuelve a indicarla, por favor.")
                context.user_data["confirmacion_pendiente"] = None
                return True
            fecha_utc = local_a_utc(fecha_local)
            await ejecutar_query(
                "INSERT INTO recordatorios (chat_id, mensaje, fecha_recordatorio, enviado) VALUES ($1, $2, $3, FALSE)",
                (chat_id, mensaje_original, fecha_utc)
            )
            fecha_mostrar = fecha_local.strftime("%d/%m/%Y %I:%M %p")
            respuesta = f"✅ Recordatorio programado para mañana, {fecha_mostrar}.\n\n📝 *{mensaje_original}*"
            await guardar_historial(chat_id, {"role": "assistant", "content": respuesta})
            await update.message.reply_text(respuesta, parse_mode="Markdown")
            context.user_data["confirmacion_pendiente"] = None
            return True

        await update.message.reply_text("⚠️ Responde 'sí' para confirmar, 'mañana' para moverlo a mañana o 'no' para cancelar.")
        return True
    except Exception as e:
        logger.error(f"❌ Error en manejar_confirmacion: {e}", exc_info=True)
        await update.message.reply_text("⚠️ Error procesando la confirmación. Intenta de nuevo.")
        context.user_data["confirmacion_pendiente"] = None
        return True

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
                "\n\nResponde con el número o el nombre exacto del proyecto."
            )
            return True

        context.user_data["esperando_seleccion"] = None

        tool_func = TOOL_FUNCTIONS.get(tool_name)
        if not tool_func:
            await update.message.reply_text("❌ Error: No encontré la herramienta.")
            return True

        try:
            result = await tool_func(**args_originales)
            chat_id = update.effective_chat.id
            if result.get("exito"):
                texto_resultado = f"✅ {result.get('mensaje', 'Acción completada.')}"
            else:
                texto_resultado = f"❌ {result.get('error', 'Error desconocido.')}"
            await guardar_historial(chat_id, {"role": "assistant", "content": texto_resultado})
            await update.message.reply_text(texto_resultado)
        except Exception as e:
            logger.error(f"Error ejecutando tool después de selección: {e}")
            await update.message.reply_text(f"❌ Error al ejecutar: {str(e)}")

        return True

    except Exception as e:
        logger.error(f"❌ Error en manejar_seleccion: {e}", exc_info=True)
        await update.message.reply_text("⚠️ Error procesando la selección. Intenta de nuevo.")
        return True

# ==================== MANEJADORES ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = update.effective_chat.id
        await limpiar_historial(chat_id)
        context.user_data["confirmacion_pendiente"] = None
        context.user_data["esperando_seleccion"] = None
        await update.message.reply_text(
            "¡Hola, jefe! 🛠️ Soy su asistente de gestión de proyectos.\n\n"
            "Puedo ayudarle a:\n"
            "- Registrar clientes y proyectos\n"
            "- Registrar pagos y anticipos\n"
            "- Consultar proyectos activos, liquidados, cancelados\n"
            "- Marcar presupuestos como enviados\n"
            "- Registrar gastos generales\n"
            "- Cancelar o borrar proyectos (con confirmación)\n"
            "- Editar información de clientes\n"
            "- Programar, consultar, editar y borrar recordatorios\n\n"
            "💡 *Nota:* Si en algún momento me confundo, solo escribe /start para reiniciarme."
        )
    except Exception as e:
        logger.error(f"❌ Error en start: {e}", exc_info=True)

async def _obtener_texto(update: Update) -> Optional[str]:
    """Devuelve el texto del mensaje, transcribiendo el audio si es necesario.
    Se usa siempre, sin importar en qué parte del flujo esté la conversación
    (mensaje normal, esperando confirmación, o esperando selección de proyecto),
    para que la voz nunca se ignore por estar en medio de una pregunta pendiente.
    Devuelve None si no se pudo obtener texto (audio vacío, sin transcriptor, error)."""
    if update.message.text:
        return update.message.text

    if update.message.voice:
        if not groq_client:
            await update.message.reply_text("❌ El servicio de transcripción de voz no está configurado.")
            return None
        try:
            await update.message.reply_text("🎙️ Escuchando...")
            voice_file = await update.message.voice.get_file()
            buffer = io.BytesIO()
            await voice_file.download_to_memory(buffer)
            texto = await asyncio.to_thread(transcribir_audio_buffer, buffer)
            if not texto:
                await update.message.reply_text("❌ No pude entender el audio. ¿Puedes repetirlo o escribirlo?")
                return None
            await update.message.reply_text(f"📝 *\"{texto}\"*", parse_mode="Markdown")
            return texto
        except Exception as e:
            logger.error(f"Error manejando audio: {e}")
            await update.message.reply_text("❌ Error al procesar el audio. Intenta de nuevo.")
            return None

    return None


async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        texto = await _obtener_texto(update)
        if texto is None:
            return  # ya se le avisó al jefe (audio vacío, sin transcriptor, o error)

        texto_lower = texto.lower()

        # Válvula de escape sin /start
        if any(palabra in texto_lower for palabra in ["cancelar", "olvida", "reiniciar", "basta", "no importa"]):
            context.user_data["confirmacion_pendiente"] = None
            context.user_data["esperando_seleccion"] = None
            await update.message.reply_text("✅ Entendido, he cancelado la operación y limpiado la memoria. ¿En qué más te ayudo, jefe?")
            return

        if context.user_data.get("confirmacion_pendiente"):
            await manejar_confirmacion(update, context, texto)
            return

        if context.user_data.get("esperando_seleccion"):
            await manejar_seleccion(update, context, texto)
            return

        await procesar_mensaje(update, context, texto)

    except Exception as e:
        logger.error(f"❌ Error en handler: {e}", exc_info=True)
        context.user_data["confirmacion_pendiente"] = None
        context.user_data["esperando_seleccion"] = None
        try:
            await update.message.reply_text("⚠️ Ocurrió un error. Por favor, intenta de nuevo.")
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
                logger.info(f"✅ Recordatorio {row['id']} enviado a {row['chat_id']}")
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
        logger.info("✅ JobQueue para recordatorios iniciado (cada 60s).")
    else:
        logger.warning("⚠️ JobQueue no disponible. Instala python-telegram-bot[job-queue] para recordatorios automáticos.")

    logger.info("🤖 Bot iniciado con correcciones anti-huérfanos, orden cronológico estricto y límite de historial=20.")
    app.run_polling()

if __name__ == "__main__":
    main()
