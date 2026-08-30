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

# Cliente asíncrono de DeepSeek
deepseek_client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# Zona horaria México
try:
    ZONA_HORARIA = pytz.timezone("America/Mexico_City")
except Exception:
    ZONA_HORARIA = None


def ahora_cdmx():
    if ZONA_HORARIA:
        return datetime.now(ZONA_HORARIA)
    return datetime.utcnow() - timedelta(hours=6)


# ==================== ZONA HORARIA CENTRALIZADA ====================
def local_a_utc(fecha_local: datetime) -> datetime:
    """Convierte una fecha local (CDMX) a UTC."""
    if ZONA_HORARIA:
        fecha_localizada = ZONA_HORARIA.localize(fecha_local)
        fecha_utc = fecha_localizada.astimezone(pytz.UTC)
        return fecha_utc.replace(tzinfo=None)
    return fecha_local + timedelta(hours=6)


def utc_a_local(fecha_utc: datetime) -> datetime:
    """Convierte una fecha UTC a local (CDMX)."""
    if ZONA_HORARIA:
        fecha_utc_localized = pytz.UTC.localize(fecha_utc)
        return fecha_utc_localized.astimezone(ZONA_HORARIA).replace(tzinfo=None)
    return fecha_utc - timedelta(hours=6)


# ==================== POOL DE CONEXIONES ASYNCPG ====================
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


# ==================== FUNCIONES DE BASE DE DATOS ====================
async def ejecutar_query(query, params=None, fetch=False):
    conn = await get_connection()
    try:
        if fetch:
            return await conn.fetch(query, *(params or ()))
        else:
            return await conn.execute(query, *(params or ()))
    finally:
        await put_connection(conn)


# ==================== CREAR TABLAS SI NO EXISTEN ====================
async def crear_tablas():
    """Crea las tablas necesarias si no existen."""
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
        """
        CREATE EXTENSION IF NOT EXISTS unaccent
        """,
    ]
    for query in queries:
        try:
            await ejecutar_query(query)
        except Exception as e:
            logger.warning(f"⚠️ Error creando tabla (puede que ya exista): {e}")


# ==================== HISTORIAL EN POSTGRES (CORREGIDO + BLINDAJE) ====================
async def guardar_historial(chat_id: int, mensaje: dict):
    """
    Guarda el mensaje COMPLETO (con 'tool_calls' o 'tool_call_id' si los tiene)
    como JSON, para poder reconstruirlo exacto como lo necesita la API después.
    """
    query = """
        INSERT INTO historial_chat (chat_id, rol, contenido)
        VALUES ($1, $2, $3)
    """
    await ejecutar_query(query, (chat_id, mensaje.get("role", "user"), json.dumps(mensaje)))


async def obtener_historial(chat_id: int, limite: int = 12) -> List[Dict]:
    """
    Obtiene los últimos mensajes y los reconstruye exactos desde JSON.
    Descarta automáticamente mensajes mal formados (sin 'role') para
    que filas viejas corruptas no rompan la conversación.
    """
    query = """
        SELECT contenido
        FROM historial_chat
        WHERE chat_id = $1
        ORDER BY fecha DESC
        LIMIT $2
    """
    resultados = await ejecutar_query(query, (chat_id, limite), fetch=True)
    mensajes = []
    for r in reversed(resultados):
        try:
            m = json.loads(r["contenido"])
        except json.JSONDecodeError:
            # Por si quedan filas viejas guardadas como texto plano
            m = {"role": "assistant", "content": r["contenido"]}

        # BLINDAJE EXTRA: si el mensaje no tiene 'role', se descarta
        if not isinstance(m, dict) or "role" not in m:
            logger.warning(f"Fila de historial descartada por formato inválido: {m}")
            continue

        mensajes.append(m)

    return mensajes


async def limpiar_historial(chat_id: int):
    await ejecutar_query("DELETE FROM historial_chat WHERE chat_id = $1", (chat_id,))


# ==================== CLIENTES ====================
async def buscar_o_crear_cliente(nombre_cliente, telefono=None, direccion=None, notas=None):
    nombre_cliente = nombre_cliente.lower().strip()
    query = "SELECT id FROM clientes WHERE unaccent(nombre) ILIKE unaccent($1)"
    result = await ejecutar_query(query, (f"%{nombre_cliente}%",), fetch=True)
    if result:
        cliente_id = result[0]["id"]
        if telefono or direccion or notas:
            updates = []
            params = []
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
                await ejecutar_query(
                    f"UPDATE clientes SET {set_clause} WHERE id = ${len(params)}",
                    params,
                )
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


# ==================== DESAMBIGUACIÓN DE PROYECTOS ====================
async def _resolver_proyecto_o_pedir(
    cliente: str,
    nombre_corto: Optional[str],
    proyectos: List[Dict],
    accion_descripcion: str = "",
) -> Tuple[Optional[Dict], Optional[Dict]]:
    if not proyectos:
        return None, {"exito": False, "error": f"No hay proyectos activos para {cliente}."}

    if nombre_corto:
        coincidencias = [
            p for p in proyectos
            if nombre_corto.lower() in (p["nombre_corto"] or "").lower()
        ]
        if len(coincidencias) == 1:
            return coincidencias[0], None
        if len(coincidencias) > 1:
            proyectos = coincidencias
        elif len(coincidencias) == 0:
            coincidencias = [
                p for p in proyectos
                if nombre_corto.lower() in (p["descripcion"] or "").lower()
            ]
            if len(coincidencias) == 1:
                return coincidencias[0], None
            if len(coincidencias) > 1:
                proyectos = coincidencias
            else:
                return None, {
                    "exito": False,
                    "error": f"No encontré un proyecto que coincida con '{nombre_corto}' para {cliente}."
                }

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
        "error": (
            f"{cliente} tiene {len(proyectos)} proyectos activos. "
            f"Pregúntale al jefe cuál es antes de continuar, y vuelve a llamar "
            f"esta misma herramienta pasando el 'nombre_corto' exacto que el jefe elija."
        ),
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
        count_query = f"""
            SELECT COUNT(*) FROM proyectos p JOIN clientes c ON p.cliente_id = c.id
            WHERE unaccent(c.nombre) ILIKE unaccent($1) AND {condicion}
        """
        total_count = await ejecutar_query(count_query, (f"%{cliente}%",), fetch=True)
        total_real = total_count[0]["count"] if total_count else 0

        query = f"""
            SELECT c.nombre, p.nombre_corto, p.descripcion, p.monto_total, p.monto_pagado, p.estado,
                   p.material_comprado, p.presupuesto_enviado, c.telefono, c.direccion
            FROM proyectos p JOIN clientes c ON p.cliente_id = c.id
            WHERE unaccent(c.nombre) ILIKE unaccent($1) AND {condicion}
            ORDER BY p.fecha_creacion DESC
            LIMIT 5
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
            ORDER BY c.nombre, p.fecha_creacion DESC
            LIMIT 5
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
    proyectos = await ejecutar_query(query, (f"%{cliente}%",), fetch=True)
    if not proyectos:
        return {"exito": False, "error": f"No hay proyectos para {cliente} que no estén cancelados."}
    proyectos_dict = []
    for row in proyectos:
        proyectos_dict.append({
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
    proyecto, aviso = await _resolver_proyecto_o_pedir(cliente, nombre_corto, proyectos_dict, "cancelar proyecto")
    if aviso:
        return aviso
    pid = proyecto["id"]
    nc = proyecto["nombre_corto"]
    await ejecutar_query("UPDATE proyectos SET estado = 'Cancelado' WHERE id = $1", (pid,))
    return {"exito": True, "mensaje": f"Proyecto '{nc}' de {cliente} ha sido cancelado."}


async def tool_borrar_proyecto(cliente: str, nombre_corto: str = None, confirmado: bool = False):
    if not confirmado:
        return {
            "exito": False,
            "error": "Se requiere confirmación explícita para borrar. Pregunta al usuario: '¿Estás seguro de borrar el proyecto?'. Responde 'SÍ' para confirmar.",
        }
    query = """
        SELECT p.id, p.nombre_corto, p.descripcion, p.monto_total, p.monto_pagado, p.estado,
               p.material_comprado, p.costo_material, p.presupuesto_enviado, c.nombre as cliente
        FROM proyectos p JOIN clientes c ON p.cliente_id = c.id
        WHERE unaccent(c.nombre) ILIKE unaccent($1)
        ORDER BY p.fecha_creacion DESC
    """
    proyectos = await ejecutar_query(query, (f"%{cliente}%",), fetch=True)
    if not proyectos:
        return {"exito": False, "error": f"No encontré proyectos para {cliente}."}
    proyectos_dict = []
    for row in proyectos:
        proyectos_dict.append({
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
    proyecto, aviso = await _resolver_proyecto_o_pedir(cliente, nombre_corto, proyectos_dict, "borrar proyecto")
    if aviso:
        return aviso
    pid = proyecto["id"]
    nc = proyecto["nombre_corto"]
    await ejecutar_query("DELETE FROM proyectos WHERE id = $1", (pid,))
    await ejecutar_query(
        "DELETE FROM clientes WHERE id NOT IN (SELECT DISTINCT cliente_id FROM proyectos WHERE cliente_id IS NOT NULL)"
    )
    return {"exito": True, "mensaje": f"Proyecto '{nc}' de {cliente} eliminado definitivamente."}


async def tool_registrar_gasto(descripcion: str, monto: float):
    if monto <= 0:
        return {"exito": False, "error": "El monto debe ser mayor a cero."}
    await ejecutar_query("INSERT INTO gastos (descripcion, monto) VALUES ($1, $2)", (descripcion, monto))
    return {"exito": True, "mensaje": f"Gasto '{descripcion}' de ${monto:.2f} registrado."}


async def tool_consultar_gastos(limite: int = 10):
    resultados = await ejecutar_query(
        "SELECT fecha, descripcion, monto FROM gastos ORDER BY fecha DESC LIMIT $1",
        (limite,),
        fetch=True,
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


# ===== RECORDATORIOS =====
async def tool_crear_recordatorio(mensaje: str, fecha_recordatorio: str, chat_id: int):
    try:
        fecha_local = datetime.strptime(fecha_recordatorio, "%Y-%m-%d %H:%M:%S")
        fecha_utc = local_a_utc(fecha_local)
        query = """
            INSERT INTO recordatorios (chat_id, mensaje, fecha_recordatorio, enviado)
            VALUES ($1, $2, $3, FALSE) RETURNING id
        """
        await ejecutar_query(query, (chat_id, mensaje, fecha_utc))
        fecha_mostrar = fecha_local.strftime("%d/%m/%Y %I:%M %p")
        return {"exito": True, "mensaje": f"🔔 Recordatorio programado para el {fecha_mostrar}.\n\n📝 *{mensaje}*"}
    except ValueError as e:
        logger.error(f"Error parseando fecha en tool_crear_recordatorio: {e}")
        return {"exito": False, "error": "Formato de fecha inválido. Usa YYYY-MM-DD HH:MM:SS."}
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
        return {"exito": True, "mensaje": "No hay recordatorios pendientes en este momento.", "data": []}
    data = []
    for r in resultados:
        fecha_local = utc_a_local(r["fecha_recordatorio"])
        data.append({
            "id_recordatorio": r["id"],
            "mensaje": r["mensaje"],
            "fecha_local": fecha_local.strftime("%Y-%m-%d %H:%M:%S")
        })
    return {"exito": True, "data": data}


async def tool_borrar_recordatorio(id_recordatorio: int, chat_id: int):
    query = "DELETE FROM recordatorios WHERE id = $1 AND chat_id = $2"
    await ejecutar_query(query, (id_recordatorio, chat_id))
    return {"exito": True, "mensaje": f"🗑️ Recordatorio con ID {id_recordatorio} eliminado."}


async def tool_editar_recordatorio(
    id_recordatorio: int,
    nuevo_mensaje: str = None,
    nueva_fecha: str = None,
    chat_id: int = None,
):
    updates, params = [], []
    if nuevo_mensaje:
        updates.append(f"mensaje = ${len(params)+1}")
        params.append(nuevo_mensaje)
    if nueva_fecha:
        try:
            fecha_local = datetime.strptime(nueva_fecha, "%Y-%m-%d %H:%M:%S")
            fecha_utc = local_a_utc(fecha_local)
            updates.append(f"fecha_recordatorio = ${len(params)+1}")
            params.append(fecha_utc)
        except ValueError:
            return {"exito": False, "error": "Formato de fecha inválido. Usa YYYY-MM-DD HH:MM:SS."}
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
            "description": "Programa un recordatorio personal. La fecha debe estar en formato YYYY-MM-DD HH:MM:SS (hora de México).",
            "parameters": {
                "type": "object",
                "properties": {
                    "mensaje": {"type": "string", "description": "Texto del recordatorio"},
                    "fecha_recordatorio": {"type": "string", "description": "Fecha y hora exacta en formato YYYY-MM-DD HH:MM:SS (hora de México)"}
                },
                "required": ["mensaje", "fecha_recordatorio"]
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_consultar_recordatorios",
            "description": "Muestra la lista de recordatorios pendientes y sus IDs. Úsalo ANTES de editar o borrar un recordatorio si el usuario no da el ID.",
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
            "description": "Cambia el mensaje o la fecha de un recordatorio existente usando su ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id_recordatorio": {"type": "integer", "description": "El ID numérico del recordatorio a editar"},
                    "nuevo_mensaje": {"type": "string", "description": "El nuevo texto del recordatorio (opcional)"},
                    "nueva_fecha": {"type": "string", "description": "La nueva fecha en formato YYYY-MM-DD HH:MM:SS (opcional)"}
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


# ==================== PROMPT DEL SISTEMA (ACTUALIZADO CON REGLAS ESTRICTAS PARA RECORDATORIOS) ====================
SYSTEM_PROMPT_BASE = (
    "Eres el asistente de gestión de proyectos de un taller de aluminio. "
    "Tu tarea es ayudar a registrar datos, consultar materiales y administrar pagos. "
    "No asumas información. Si el usuario te pide registrar un gasto o un pago, pero falta la cantidad, el concepto o el proyecto, PREGÚNTALE en lenguaje natural antes de ejecutar la herramienta. "
    "Solo ejecuta herramientas de base de datos cuando tengas toda la información requerida explícita en la conversación. "
    "Si el usuario te insiste en ejecutar una acción, vuelve a utilizar la herramienta correspondiente, ignorando fallos previos en la base de datos. No te excusas con errores pasados si el usuario te pide explícitamente que lo intentes de nuevo. "
    "Habla de forma directa y clara, usando 'jefe' o 'patrón' ocasionalmente. "
    "Cuando muestres listas, preséntalas de manera ordenada, con emojis para facilitar la lectura. "
    "Si el usuario pide borrar algo, siempre pregunta confirmación primero, y solo ejecuta la herramienta cuando el usuario confirme explícitamente. "
    # ===== NUEVAS REGLAS ESTRICTAS PARA RECORDATORIOS =====
    "REGLA ESTRICTA PARA RECORDATORIOS: Cada vez que el usuario te pida que le recuerdes algo (ej. 'recuérdame a las X...', 'pon un aviso...'), DEBES SIEMPRE crear un registro NUEVO usando tool_crear_recordatorio. "
    "NUNCA asumas que quiere modificar un recordatorio anterior solo por el contexto de la plática. "
    "ÚNICAMENTE vas a usar tool_editar_recordatorio si el usuario utiliza verbos explícitos de cambio como 'edita', 'cambia', 'modifica' o 'mueve' un recordatorio. "
    "Si el usuario pide editar o borrar, ejecuta PRIMERO tool_consultar_recordatorios de forma silenciosa para encontrar el ID correcto. Si el usuario no especifica cuál, muéstrale la lista de pendientes y pídele el ID."
)


# ==================== PODA DE HISTORIAL ====================
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


# ==================== BUCLE PRINCIPAL ====================
async def procesar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str):
    if not texto:
        await update.message.reply_text("No entendí el mensaje. ¿Puedes repetirlo?")
        return

    chat_id = update.effective_chat.id

    # Guardar mensaje del usuario (completo)
    await guardar_historial(chat_id, {"role": "user", "content": texto})

    historial = await obtener_historial(chat_id, 15)
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
                max_tokens=800,
            )
        except Exception as e:
            logger.error(f"Error llamando a DeepSeek: {e}")
            await update.message.reply_text("❌ Error al comunicarme con el asistente. Intenta de nuevo.")
            return

        message = response.choices[0].message
        if not message.tool_calls:
            respuesta = message.content
            if respuesta:
                await guardar_historial(chat_id, {"role": "assistant", "content": respuesta})
                await update.message.reply_text(respuesta, parse_mode="Markdown")
            else:
                await update.message.reply_text("No tengo respuesta para eso.")
            return

        tool_calls = message.tool_calls
        # Guardar el mensaje del asistente con tool_calls (completo)
        mensaje_asistente = {
            "role": "assistant",
            "content": message.content,
            "tool_calls": [tc.model_dump() for tc in tool_calls],
        }
        await guardar_historial(chat_id, mensaje_asistente)

        for tool_call in tool_calls:
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

            # Guardar el mensaje de tool con tool_call_id (completo)
            await guardar_historial(chat_id, {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": json.dumps(result),
            })

            if result.get("requiere_seleccion"):
                opciones = result.get("opciones", [])
                mensaje_opciones = f"{result.get('error', '')}\n\n"
                for i, opcion in enumerate(opciones, 1):
                    mensaje_opciones += f"{i}. {opcion}\n"
                mensaje_opciones += "\nResponde con el nombre exacto del proyecto o el número."
                await update.message.reply_text(mensaje_opciones, parse_mode="Markdown")
                context.user_data["esperando_seleccion"] = {
                    "tool_name": function_name,
                    "args_originales": function_args,
                    "opciones": opciones,
                    "cliente": function_args.get("cliente", ""),
                }
                return

        historial = await obtener_historial(chat_id, 15)
        historial_podado = podar_historial(historial)
        mensajes_api = [system_msg] + historial_podado

    await update.message.reply_text("El proceso ha tomado demasiados pasos. Por favor, simplifica tu solicitud.")


# ==================== MANEJO DE RESPUESTA DE SELECCIÓN ====================
async def manejar_seleccion(update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str):
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

        # Después de selección, guardamos el resultado como un mensaje de assistant
        # (porque no hay un tool_call_id real, es una re-ejecución manual)
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


# ==================== MANEJADORES DE TELEGRAM ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await limpiar_historial(chat_id)
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


async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if context.user_data.get("esperando_seleccion"):
        if update.message.text:
            await manejar_seleccion(update, context, update.message.text)
        else:
            await update.message.reply_text("⚠️ Por favor, responde con texto para seleccionar el proyecto.")
        return

    if update.message.text:
        texto = update.message.text
        await procesar_mensaje(update, context, texto)
    elif update.message.voice:
        if not groq_client:
            await update.message.reply_text("❌ El servicio de transcripción de voz no está configurado.")
            return
        try:
            await update.message.reply_text("🎙️ Escuchando...")
            voice_file = await update.message.voice.get_file()
            buffer = io.BytesIO()
            await voice_file.download_to_memory(buffer)
            texto = await asyncio.to_thread(transcribir_audio_buffer, buffer)
            if not texto:
                await update.message.reply_text("❌ No pude entender el audio. ¿Puedes repetirlo o escribirlo?")
                return
            await update.message.reply_text(f"📝 *\"{texto}\"*", parse_mode="Markdown")
            await procesar_mensaje(update, context, texto)
        except Exception as e:
            logger.error(f"Error manejando audio: {e}")
            await update.message.reply_text("❌ Error al procesar el audio. Intenta de nuevo.")


# ==================== MOTOR DE ENVÍO DE RECORDATORIOS ====================
async def checar_recordatorios(context: ContextTypes.DEFAULT_TYPE):
    query = """
        SELECT id, chat_id, mensaje, fecha_recordatorio
        FROM recordatorios
        WHERE enviado = FALSE AND fecha_recordatorio <= (NOW() AT TIME ZONE 'UTC')
    """
    pendientes = await ejecutar_query(query, fetch=True)
    for row in pendientes:
        mensaje = f"🔔 *RECORDATORIO:*\n{row['mensaje']}"
        try:
            await context.bot.send_message(
                chat_id=row['chat_id'],
                text=mensaje,
                parse_mode="Markdown"
            )
            await ejecutar_query(
                "UPDATE recordatorios SET enviado = TRUE WHERE id = $1",
                (row['id'],)
            )
            logger.info(f"Recordatorio {row['id']} enviado a {row['chat_id']}")
        except Exception as e:
            logger.error(f"Error enviando recordatorio {row['id']}: {e}")


# ==================== INICIO ====================
def main():
    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_db_pool())
    loop.run_until_complete(crear_tablas())

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handler))
    app.add_handler(MessageHandler(filters.VOICE, handler))

    job_queue = app.job_queue
    if job_queue:
        job_queue.run_repeating(checar_recordatorios, interval=60, first=10)
        logger.info("✅ JobQueue para recordatorios iniciado (cada 60s).")
    else:
        logger.warning("⚠️ JobQueue no disponible. Los recordatorios no se enviarán automáticamente. Instala `pip install python-telegram-bot[job-queue]`")

    logger.info("🤖 Bot asíncrono con asyncpg, desambiguación de proyectos, historial en Postgres (con blindaje anti-corrupción) y reglas estrictas para recordatorios iniciado.")
    app.run_polling()


if __name__ == "__main__":
    main()
