"""
Asistente Inteligente - Taller de Aluminio
===========================================

CORRECCIÓN PUNTUAL APLICAR MANUALMENTE (una sola vez):
  UPDATE proyectos
  SET estado = 'Por cobrar'
  WHERE estado = 'Presupuesto enviado'
    AND monto_pagado > 0
    AND cliente_id = (SELECT id FROM clientes WHERE unaccent(nombre) ILIKE unaccent('%Diego Rivera%'));
"""

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
    await ejecutar_query(
        query, (chat_id, mensaje.get("role", "user"), json.dumps(mensaje))
    )


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
    await ejecutar_query(
        "DELETE FROM historial_chat WHERE chat_id = $1", (chat_id,)
    )


# ==================== CLIENTES Y PROYECTOS ====================
async def buscar_o_crear_cliente(
    nombre_cliente, telefono=None, direccion=None, notas=None
):
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
                await ejecutar_query(
                    f"UPDATE clientes SET {set_clause} WHERE id = ${len(params)}", params
                )
        return cliente_id
    else:
        insert = """
            INSERT INTO clientes (nombre, telefono, direccion, notas_adicionales)
            VALUES ($1, $2, $3, $4) RETURNING id
        """
        result = await ejecutar_query(
            insert, (nombre_cliente, telefono, direccion, notas), fetch=True
        )
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
        return None, {
            "exito": False,
            "error": f"No hay proyectos activos para {cliente}.",
        }
    if nombre_corto:
        coincidencias = [
            p
            for p in proyectos
            if nombre_corto.lower() in (p["nombre_corto"] or "").lower()
        ]
        if len(coincidencias) == 1:
            return coincidencias[0], None
        if len(coincidencias) > 1:
            proyectos = coincidencias
        elif len(coincidencias) == 0:
            coincidencias = [
                p
                for p in proyectos
                if nombre_corto.lower() in (p["descripcion"] or "").lower()
            ]
            if len(coincidencias) == 1:
                return coincidencias[0], None
            if len(coincidencias) > 1:
                proyectos = coincidencias
            else:
                return None, {
                    "exito": False,
                    "error": f"No encontré proyecto para '{nombre_corto}' en {cliente}.",
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
        "error": f"{cliente} tiene {len(proyectos)} proyectos activos. Pregunta cuál y vuelve a llamar con 'nombre_corto'.",
    }


def _extraer_campos_proyecto(proyecto: Dict) -> Tuple:
    """Extrae los campos de un proyecto en orden consistente."""
    return (
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
    result = await ejecutar_query(
        insert,
        (cliente_id, nombre_corto or "Proyecto General", descripcion, monto, notas),
        fetch=True,
    )
    return {
        "exito": True,
        "mensaje": f"Proyecto '{nombre_corto}' registrado para {cliente}. ID: {result[0]['id']}",
    }


async def tool_registrar_pago(cliente: str, monto: float, referencia: str = None):
    if monto <= 0:
        return {"exito": False, "error": "El monto debe ser mayor a cero."}
    proyectos = await obtener_proyectos_activos(cliente)
    if not proyectos:
        return {"exito": False, "error": f"No hay proyectos activos para {cliente}."}
    proyecto, aviso = await _resolver_proyecto_o_pedir(
        cliente, referencia, proyectos, "registrar pago"
    )
    if aviso:
        return aviso
    pid, nc, desc, total, pagado, estado, *_ = _extraer_campos_proyecto(proyecto)

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


# =====================================================================
# PATCH: tool_marcar_presupuesto_enviado — ya NO regresa el estado
# hacia atrás. Solo cambia estado a "Presupuesto enviado" si el proyecto
# estaba en "Pendiente de cotizar". En cualquier otro estado avanzado
# (En proceso, Por cobrar) solo marca el flag presupuesto_enviado.
# =====================================================================
async def tool_marcar_presupuesto_enviado(
    cliente: str,
    nombre_corto: str = None,
    monto: float = None,
    descripcion: str = None,
):
    proyectos = await obtener_proyectos_activos(cliente)
    if not proyectos:
        return {"exito": False, "error": f"No hay proyectos activos para {cliente}."}
    proyecto, aviso = await _resolver_proyecto_o_pedir(
        cliente, nombre_corto, proyectos, "marcar presupuesto enviado"
    )
    if aviso:
        return aviso
    pid, nc, desc, total, pagado, estado_actual, *_ = _extraer_campos_proyecto(
        proyecto
    )

    updates = [
        "presupuesto_enviado = TRUE",
        "fecha_presupuesto = CURRENT_TIMESTAMP",
    ]
    params = []

    # Solo avanza el estado si está en el estado inicial
    if estado_actual == "Pendiente de cotizar":
        updates.append("estado = 'Presupuesto enviado'")

    if monto is not None and monto > 0:
        updates.append(f"monto_total = ${len(params)+1}")
        params.append(monto)
    if descripcion:
        updates.append(f"descripcion = COALESCE(${len(params)+1}, descripcion)")
        params.append(descripcion)
    params.append(pid)
    await ejecutar_query(
        f"UPDATE proyectos SET {', '.join(updates)} WHERE id = ${len(params)}", params
    )
    return {
        "exito": True,
        "mensaje": f"Presupuesto marcado como enviado para '{nc}' de {cliente}.",
    }


# =====================================================================
# NUEVA TOOL: tool_marcar_material_comprado
# Escribe directamente en las columnas material_comprado,
# costo_material y fecha_compra_material del proyecto.
# =====================================================================
async def tool_marcar_material_comprado(
    cliente: str,
    nombre_corto: str = None,
    comprado: bool = True,
    costo_material: float = None,
):
    proyectos = await obtener_proyectos_activos(cliente)
    if not proyectos:
        return {"exito": False, "error": f"No hay proyectos activos para {cliente}."}
    proyecto, aviso = await _resolver_proyecto_o_pedir(
        cliente, nombre_corto, proyectos, "marcar material comprado"
    )
    if aviso:
        return aviso
    pid, nc, desc, total, pagado, estado, *_ = _extraer_campos_proyecto(proyecto)

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
    await ejecutar_query(
        f"UPDATE proyectos SET {', '.join(updates)} WHERE id = ${len(params)}", params
    )

    estado_texto = "comprado ✅" if comprado else "pendiente ⏳"
    costo_texto = (
        f" (costo: ${costo_material:.2f})" if (comprado and costo_material) else ""
    )
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
        condicion = (
            "p.estado IN ('Por cobrar', 'En proceso') AND p.monto_total > p.monto_pagado"
        )
    else:
        return {"exito": False, "error": "Tipo de consulta no válido."}
    if cliente:
        cliente = cliente.lower().strip()
        count_query = f"SELECT COUNT(*) FROM proyectos p JOIN clientes c ON p.cliente_id = c.id WHERE unaccent(c.nombre) ILIKE unaccent($1) AND {condicion}"
        total_count = await ejecutar_query(
            count_query, (f"%{cliente}%",), fetch=True
        )
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
            ORDER BY c.nome, p.fecha_creacion DESC LIMIT 5
        """
        resultados = await ejecutar_query(query, fetch=True)
    if not resultados:
        return {"exito": True, "mensaje": "No hay proyectos en este estado.", "data": []}
    data = []
    for row in resultados:
        data.append(
            {
                "cliente": row["nombre"],
                "proyecto": row["nombre_corto"] or "General",
                "descripcion": row["descripcion"],
                "total": float(row["monto_total"]),
                "pagado": float(row["monto_pagado"]),
                "saldo": float(row["monto_total"]) - float(row["monto_pagado"]),
                "estado": row["estado"],
                "material_comprado": row["material_comprado"],
                "costo_material": float(row["costo_material"])
                if row["costo_material"]
                else None,
                "presupuesto_enviado": row["presupuesto_enviado"],
                "telefono": row["telefono"],
                "direccion": row["direccion"],
            }
        )
    mensaje = (
        f"Mostrando {len(data)} de {total_real} proyectos."
        if total_real > len(data)
        else None
    )
    return {"exito": True, "data": data, "mensaje": mensaje}


async def tool_cerrar_proyecto(cliente: str, nombre_corto: str = None):
    proyectos = await obtener_proyectos_activos(cliente)
    if not proyectos:
        return {"exito": False, "error": f"No hay proyectos activos para {cliente}."}
    proyecto, aviso = await _resolver_proyecto_o_pedir(
        cliente, nombre_corto, proyectos, "cerrar/liquidar proyecto"
    )
    if aviso:
        return aviso
    pid, nc, desc, total, pagado, estado, *_ = _extraer_campos_proyecto(proyecto)
    saldo = total - pagado
    if saldo <= 0:
        await ejecutar_query(
            "UPDATE proyectos SET estado = 'Liquidado' WHERE id = $1", (pid,)
        )
        return {
            "exito": True,
            "mensaje": f"Proyecto '{nc}' de {cliente} liquidado (saldo $0).",
        }
    else:
        return {
            "exito": False,
            "error": f"El proyecto '{nc}' tiene saldo pendiente de ${saldo:.2f}. Primero registra el pago restante.",
        }


async def tool_cancelar_proyecto(cliente: str, nombre_corto: str = None):
    query = """
        SELECT p.id, p.nombre_corto, p.descripcion, p.monto_total, p.monto_pagado, p.estado,
               p.material_comprado, p.costo_material, p.presupuesto_enviado, c.nombre as cliente
        FROM proyectos p JOIN clientes c ON p.cliente_id = c.id
        WHERE unaccent(c.nombre) ILIKE unaccent($1) AND p.estado != 'Cancelado'
        ORDER BY p.fecha_creacion DESC
    """
    proyectos_raw = await ejecutar_query(
        query, (f"%{cliente}%",), fetch=True
    )
    if not proyectos_raw:
        return {
            "exito": False,
            "error": f"No hay proyectos para {cliente} que no estén cancelados.",
        }
    proyectos = [dict(row) for row in proyectos_raw]
    proyecto, aviso = await _resolver_proyecto_o_pedir(
        cliente, nombre_corto, proyectos, "cancelar proyecto"
    )
    if aviso:
        return aviso
    pid = proyecto["id"]
    nc = proyecto["nombre_corto"]
    await ejecutar_query(
        "UPDATE proyectos SET estado = 'Cancelado' WHERE id = $1", (pid,)
    )
    return {
        "exito": True,
        "mensaje": f"Proyecto '{nc}' de {cliente} ha sido cancelado.",
    }


async def tool_borrar_proyecto(
    cliente: str, nombre_corto: str = None, confirmado: bool = False
):
    if not confirmado:
        return {
            "exito": False,
            "error": "Se requiere confirmación explícita para borrar. Responde 'SÍ' para confirmar.",
        }
    query = """
        SELECT p.id, p.nombre_corto, p.descripcion, p.monto_total, p.monto_pagado, p.estado,
               p.material_comprado, p.costo_material, p.presupuesto_enviado, c.nombre as cliente
        FROM proyectos p JOIN clientes c ON p.cliente_id = c.id
        WHERE unaccent(c.nombre) ILIKE unaccent($1)
        ORDER BY p.fecha_creacion DESC
    """
    proyectos_raw = await ejecutar_query(
        query, (f"%{cliente}%",), fetch=True
    )
    if not proyectos_raw:
        return {"exito": False, "error": f"No encontré proyectos para {cliente}."}
    proyectos = [dict(row) for row in proyectos_raw]
    proyecto, aviso = await _resolver_proyecto_o_pedir(
        cliente, nombre_corto, proyectos, "borrar proyecto"
    )
    if aviso:
        return aviso
    pid = proyecto["id"]
    nc = proyecto["nombre_corto"]
    await ejecutar_query("DELETE FROM proyectos WHERE id = $1", (pid,))
    await ejecutar_query(
        "DELETE FROM clientes WHERE id NOT IN (SELECT DISTINCT cliente_id FROM proyectos WHERE cliente_id IS NOT NULL)"
    )
    return {
        "exito": True,
        "mensaje": f"Proyecto '{nc}' de {cliente} eliminado definitivamente.",
    }


async def tool_registrar_gasto(descripcion: str, monto: float):
    if monto <= 0:
        return {"exito": False, "error": "El monto debe ser mayor a cero."}
    await ejecutar_query(
        "INSERT INTO gastos (descripcion, monto) VALUES ($1, $2)", (descripcion, monto)
    )
    return {
        "exito": True,
        "mensaje": f"Gasto '{descripcion}' de ${monto:.2f} registrado.",
    }


async def tool_consultar_gastos(limite: int = 10):
    resultados = await ejecutar_query(
        "SELECT fecha, descripcion, monto FROM gastos ORDER BY fecha DESC LIMIT $1",
        (limite,),
        fetch=True,
    )
    if not resultados:
        return {"exito": True, "data": [], "mensaje": "No hay gastos registrados."}
    data = [
        {
            "fecha": r["fecha"].strftime("%d/%m %H:%M"),
            "descripcion": r["descripcion"],
            "monto": float(r["monto"]),
        }
        for r in resultados
    ]
    return {"exito": True, "data": data}


async def tool_explicar_estado(cliente: str, nombre_corto: str = None):
    proyectos = await obtener_proyectos_activos(cliente)
    if not proyectos:
        return {"exito": False, "error": f"No hay proyectos activos para {cliente}."}
    proyecto, aviso = await _resolver_proyecto_o_pedir(
        cliente, nombre_corto, proyectos, "explicar estado"
    )
    if aviso:
        return aviso
    pid, nc, desc, total, pagado, estado, mat_comp, costo_mat, presup, cliente_nombre = (
        _extraer_campos_proyecto(proyecto)
    )
    saldo = total - pagado
    explicacion = (
        f"Proyecto '{nc}' de {cliente}: Estado '{estado}', "
        f"Monto total ${total:.2f}, Pagado ${pagado:.2f}, Saldo ${saldo:.2f}."
    )
    if estado == "Liquidado":
        explicacion += " Está liquidado porque el saldo es cero."
    elif estado == "Por cobrar":
        explicacion += " Está pendiente de cobro porque el saldo es positivo."
    elif estado == "En proceso":
        explicacion += " Está en proceso (se ha recibido algún anticipo)."
    return {"exito": True, "mensaje": explicacion}


async def tool_editar_cliente(
    cliente: str, telefono: str = None, direccion: str = None, notas: str = None
):
    query_buscar = "SELECT id, nombre FROM clientes WHERE unaccent(nombre) ILIKE unaccent($1)"
    resultado = await ejecutar_query(
        query_buscar, (f"%{cliente.lower().strip()}%",), fetch=True
    )
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
        return {
            "exito": False,
            "error": "No se proporcionaron datos nuevos para actualizar.",
        }
    params.append(cliente_id)
    await ejecutar_query(
        f"UPDATE clientes SET {', '.join(updates)} WHERE id = ${len(params)}", params
    )
    return {
        "exito": True,
        "mensaje": f"Datos actualizados correctamente para el cliente '{nombre_real}'.",
    }


# ==================== RECORDATORIOS ====================
def _normalizar_texto_hora(texto: str) -> str:
    texto = texto.lower().strip()
    reemplazos = {
        "a.m.": "am",
        "p.m.": "pm",
        "a. m.": "am",
        "p. m.": "pm",
        "a m": "am",
        "p m": "pm",
        "mediodia": "mediodía",
        "medianoche": "00:00",
    }
    for a, b in reemplazos.items():
        texto = texto.replace(a, b)
    return texto


def interpretar_fecha(
    fecha_texto: str,
) -> Tuple[Optional[str], Optional[str], Optional[datetime]]:
    original = fecha_texto or ""
    texto = _normalizar_texto_hora(original)
    hoy = ahora_cdmx()
    fecha_actual = hoy.strftime("%Y-%m-%d")

    if re.search(r"\bpasado mañana\b", texto):
        fecha_base = hoy + timedelta(days=2)
    elif re.search(r"\bmañana\b|\bmanana\b", texto):
        fecha_base = hoy + timedelta(days=1)
    else:
        fecha_base = hoy

    fecha_str = fecha_actual
    m_iso = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", texto)
    m_lat = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](20\d{2})\b", texto)
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

    tiene_am = bool(re.search(r"\b(?:am|a\.m\.)\b", texto))
    tiene_pm = bool(re.search(r"\b(?:pm|p\.m\.)\b", texto))
    es_madrugada = bool(re.search(r"\b(?:madrugada)\b", texto))
    es_manana = bool(re.search(r"\b(?:mañana|manana)\b", texto))
    es_tarde = bool(re.search(r"\b(?:tarde)\b", texto))
    es_noche = bool(re.search(r"\b(?:noche)\b", texto))
    contexto_inequivoco = (
        tiene_am or tiene_pm or es_madrugada or es_manana or es_tarde or es_noche
    )

    hora = minuto = None
    patrones = [
        r"\b(\d{1,2})\s*[:.,]\s*(\d{1,2})\b",
        r"\b(\d{1,2})\s+(?:y|con)\s+(\d{1,2})\b",
        r"\b(\d{1,2})\s+(?:y\s+)?media\b",
    ]
    for patron in patrones:
        m = re.search(patron, texto)
        if m:
            hora = int(m.group(1))
            minuto = 30 if len(m.groups()) == 1 else int(m.group(2))
            break

    if hora is None:
        m = re.search(r"\b(\d{1,2})\s+(\d{2})\b", texto)
        if m:
            hora, minuto = int(m.group(1)), int(m.group(2))
        else:
            m = re.search(r"\b(?:a\s+las|a\s+la|las|la)\s+(\d{1,2})\b", texto)
            if m:
                hora, minuto = int(m.group(1)), 0
            else:
                nums = re.findall(r"\b\d{1,2}\b", texto)
                if nums:
                    candidato = int(nums[0])
                    if 0 <= candidato <= 23:
                        hora, minuto = candidato, 0

    if hora is None:
        return (
            None,
            "No encontré una hora. Por favor, dime la hora, por ejemplo: '3:30 AM'.",
            None,
        )
    if minuto is None:
        minuto = 0
    if hora > 23 or minuto > 59:
        return (
            None,
            "La hora indicada no es válida. Usa una hora entre 00:00 y 23:59.",
            None,
        )

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
            return (
                None,
                "Para esa hora necesito saber si es de la mañana o de la tarde.",
                None,
            )
    elif es_manana:
        if hora == 12:
            hora = 0
    elif es_tarde or es_noche:
        if hora < 12:
            hora += 12
    else:
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


async def tool_crear_recordatorio(
    mensaje: str, fecha_recordatorio: str, chat_id: int
):
    try:
        fecha_normalizada, ambiguedad, fecha_local = interpretar_fecha(
            fecha_recordatorio
        )
        if not fecha_normalizada or not fecha_local:
            return {
                "exito": False,
                "error": ambiguedad
                or f"⚠️ No pude interpretar la fecha: '{fecha_recordatorio}'.",
            }

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
        return {
            "exito": True,
            "mensaje": "No hay recordatorios pendientes en este momento.",
            "data": [],
        }
    data = []
    for r in resultados:
        fecha_local = utc_a_local(r["fecha_recordatorio"])
        data.append(
            {
                "id_recordatorio": r["id"],
                "mensaje": r["mensaje"],
                "fecha_local": fecha_local.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    return {"exito": True, "data": data}


async def tool_borrar_recordatorio(id_recordatorio: int, chat_id: int):
    result = await ejecutar_query(
        "DELETE FROM recordatorios WHERE id = $1 AND chat_id = $2",
        (id_recordatorio, chat_id),
    )
    if not result or result.endswith(" 0"):
        return {
            "exito": False,
            "error": f"No encontré un recordatorio pendiente con ID {id_recordatorio}.",
        }
    return {
        "exito": True,
        "mensaje": f"🗑️ Recordatorio con ID {id_recordatorio} eliminado.",
    }


async def tool_editar_recordatorio(
    id_recordatorio: int,
    nuevo_mensaje: str = None,
    nueva_fecha: str = None,
    chat_id: int = None,
):
    if id_recordatorio is None:
        return {
            "exito": False,
            "error": "❌ Para editar un recordatorio debes proporcionar el ID exacto.",
        }

    if chat_id is not None:
        existe = await ejecutar_query(
            "SELECT id, mensaje, fecha_recordatorio FROM recordatorios WHERE id = $1 AND chat_id = $2 AND enviado = FALSE",
            (id_recordatorio, chat_id),
            fetch=True,
        )
    else:
        existe = await ejecutar_query(
            "SELECT id, mensaje, fecha_recordatorio FROM recordatorios WHERE id = $1 AND enviado = FALSE",
            (id_recordatorio,),
            fetch=True,
        )
    if not existe:
        return {
            "exito": False,
            "error": f"No encontré un recordatorio pendiente con ID {id_recordatorio}.",
        }

    updates, params = [], []
    if nuevo_mensaje is not None and str(nuevo_mensaje).strip():
        updates.append(f"mensaje = ${len(params)+1}")
        params.append(str(nuevo_mensaje).strip())

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
    return {
        "exito": True,
        "mensaje": f"✏️ Recordatorio {id_recordatorio} actualizado correctamente.",
    }


# ==================== DEFINICIÓN DE TOOLS (16 tools) ====================
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
                    "nombre_corto": {
                        "type": "string",
                        "description": "Nombre corto del proyecto",
                    },
                    "descripcion": {
                        "type": "string",
                        "description": "Descripción del trabajo",
                    },
                    "monto": {
                        "type": "number",
                        "description": "Presupuesto total del proyecto",
                    },
                    "telefono": {
                        "type": "string",
                        "description": "Teléfono del cliente (opcional)",
                    },
                    "direccion": {
                        "type": "string",
                        "description": "Dirección del cliente (opcional)",
                    },
                    "notas": {
                        "type": "string",
                        "description": "Notas adicionales (opcional)",
                    },
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
                    "monto": {
                        "type": "number",
                        "description": "Monto del pago (debe ser > 0)",
                    },
                    "referencia": {
                        "type": "string",
                        "description": "Nombre corto del proyecto (opcional, para desambiguar si hay varios)",
                    },
                },
                "required": ["cliente", "monto"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_marcar_presupuesto_enviado",
            "description": "Marca el flag de presupuesto enviado en un proyecto. NO cambia el estado si el proyecto ya avanzó (tiene pagos). Solo cambia el estado a 'Presupuesto enviado' si estaba en 'Pendiente de cotizar'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cliente": {"type": "string", "description": "Nombre del cliente"},
                    "nombre_corto": {
                        "type": "string",
                        "description": "Nombre corto del proyecto (opcional si solo tiene uno)",
                    },
                    "monto": {
                        "type": "number",
                        "description": "Nuevo monto total (opcional)",
                    },
                    "descripcion": {
                        "type": "string",
                        "description": "Nueva descripción (opcional)",
                    },
                },
                "required": ["cliente"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_marcar_material_comprado",
            "description": "Marca el material de un proyecto como comprado o pendiente. Escribe directamente en la columna 'material_comprado' del proyecto. Opcionalmente registra el costo del material.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cliente": {"type": "string", "description": "Nombre del cliente"},
                    "nombre_corto": {
                        "type": "string",
                        "description": "Nombre corto del proyecto (opcional si solo tiene uno)",
                    },
                    "comprado": {
                        "type": "boolean",
                        "description": "True para marcar como comprado, False para pendiente (por defecto True)",
                    },
                    "costo_material": {
                        "type": "number",
                        "description": "Costo del material comprado (opcional)",
                    },
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
                    "tipo": {
                        "type": "string",
                        "enum": ["activos", "liquidados", "cancelados", "deudores"],
                        "description": "Tipo de consulta",
                    },
                    "cliente": {
                        "type": "string",
                        "description": "Nombre del cliente (opcional) para filtrar",
                    },
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
                    "nombre_corto": {
                        "type": "string",
                        "description": "Nombre corto del proyecto (opcional si solo tiene uno)",
                    },
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
                    "nombre_corto": {
                        "type": "string",
                        "description": "Nombre corto del proyecto (opcional si solo tiene uno)",
                    },
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
                    "nombre_corto": {
                        "type": "string",
                        "description": "Nombre corto del proyecto (opcional si solo tiene uno)",
                    },
                    "confirmado": {
                        "type": "boolean",
                        "description": "Debe ser true solo si el usuario confirmó explícitamente con 'sí'",
                    },
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
                    "descripcion": {
                        "type": "string",
                        "description": "Descripción del gasto",
                    },
                    "monto": {
                        "type": "number",
                        "description": "Monto del gasto (debe ser > 0)",
                    },
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
                    "limite": {
                        "type": "integer",
                        "description": "Número de gastos a mostrar (por defecto 10)",
                    },
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
                    "nombre_corto": {
                        "type": "string",
                        "description": "Nombre corto del proyecto (opcional si solo tiene uno)",
                    },
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
                    "cliente": {
                        "type": "string",
                        "description": "Nombre del cliente a editar",
                    },
                    "telefono": {
                        "type": "string",
                        "description": "Nuevo número de teléfono (opcional)",
                    },
                    "direccion": {
                        "type": "string",
                        "description": "Nueva dirección (opcional)",
                    },
                    "notas": {
                        "type": "string",
                        "description": "Nuevas notas (opcional)",
                    },
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
                    "mensaje": {
                        "type": "string",
                        "description": "Texto del recordatorio",
                    },
                    "fecha_recordatorio": {
                        "type": "string",
                        "description": "Fecha y hora en formato informal (ej. 'hoy a las 2 con 35 de la mañana')",
                    },
                },
                "required": ["mensaje", "fecha_recordatorio"],
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
                    "id_recordatorio": {
                        "type": "integer",
                        "description": "El ID numérico del recordatorio a borrar",
                    }
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
                    "id_recordatorio": {
                        "type": "integer",
                        "description": "El ID numérico del recordatorio a editar (OBLIGATORIO)",
                    },
                    "nuevo_mensaje": {
                        "type": "string",
                        "description": "El nuevo texto del recordatorio (opcional)",
                    },
                    "nueva_fecha": {
                        "type": "string",
                        "description": "La nueva fecha en formato informal (opcional)",
                    },
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
    "para no duplicar la pregunta y hacer esperar al jefe dos veces por lo mismo. "
    "REGLA DE HERRAMIENTAS: "
    "Si el usuario te pide hacer algo y NO tienes una herramienta específica para ello, "
    "dilo claramente ('jefe, no tengo forma de hacer eso') en vez de usar otra herramienta "
    "que no corresponde. NUNCA improvises usando una herramienta para algo que no fue diseñada. "
    "Ejemplo: si quiere marcar material comprado, usa tool_marcar_material_comprado, NO "
    "tool_marcar_presupuesto_enviado ni tool_editar_cliente."
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
        if not isinstance(m, dict) or m.get("role") not in {
            "user",
            "assistant",
            "tool",
        }:
            i += 1
            continue
        if m.get("role") == "assistant" and m.get("tool_calls"):
            ids = [
                tc.get("id")
                for tc in m.get("tool_calls", [])
                if tc.get("id")
            ]
            tools = []
            j = i + 1
            while j < len(messages) and messages[j].get("role") == "tool":
                if messages[j].get("tool_call_id") in ids:
                    tools.append(messages[j])
                j += 1
            found = {x.get("tool_call_id") for x in tools}
            if not ids or not all(x in found for x in ids):
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

    if len(salida) > MAX_HISTORIAL:
        salida = salida[-MAX_HISTORIAL:]
    return salida


# ==================== TRANSCRIPCIÓN DE VOZ ====================
async def transcribir_audio(voice) -> Optional[str]:
    """Transcribe un mensaje de voz usando Groq Whisper."""
    if not groq_client:
        return None
    try:
        with io.BytesIO() as audio_buf:
            await voice.get_file().download_to_memory(audio_buf)
            audio_buf.seek(0)
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
                tmp.write(audio_buf.read())
                tmp_path = tmp.name
        with open(tmp_path, "rb") as audio_file:
            resultado = groq_client.audio.transcriptions.create(
                model="whisper-large-v3",
                file=audio_file,
                language="es",
                response_format="text",
            )
        os.unlink(tmp_path)
        return resultado.strip() if resultado else None
    except Exception as e:
        logger.error(f"Error transcribiendo audio: {e}", exc_info=True)
        return None


# ==================== LLAMADA AL LLM ====================
async def llamar_llm(chat_id: int, historial: List[Dict]) -> Dict:
    """Llama a DeepSeek con el historial podado y las tools."""
    mensajes = podar_historial(historial)
    system_msg = {"role": "system", "content": SYSTEM_PROMPT_BASE}
    payload = [system_msg] + mensajes

    try:
        response = await deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=payload,
            tools=TOOLS,
            tool_choice="auto",
            temperature=0.1,
            max_tokens=1024,
        )
        return response.choices[0].message.model_dump()
    except Exception as e:
        logger.error(f"Error llamando a DeepSeek: {e}", exc_info=True)
        return {"role": "assistant", "content": "⚠️ Tuve un problema al procesar tu solicitud. Intenta de nuevo, jefe."}


# ==================== EJECUTAR TOOLS ====================
async def ejecutar_tool_call(tool_call: Dict, chat_id: int) -> Dict:
    """Ejecuta una llamada a tool y devuelve el resultado como dict."""
    nombre = tool_call.get("function", {}).get("name", "")
    try:
        args = json.loads(tool_call.get("function", {}).get("arguments", "{}"))
    except json.JSONDecodeError:
        return {
            "exito": False,
            "error": "Error al leer los parámetros de la herramienta.",
        }

    func = TOOL_FUNCTIONS.get(nombre)
    if not func:
        return {"exito": False, "error": f"Herramienta '{nombre}' no encontrada."}

    # Inyectar chat_id si la función lo necesita
    import inspect
    sig = inspect.signature(func)
    if "chat_id" in sig.parameters and "chat_id" not in args:
        args["chat_id"] = chat_id

    try:
        resultado = await func(**args)
        return resultado if isinstance(resultado, dict) else {"exito": True, "mensaje": str(resultado)}
    except Exception as e:
        logger.error(f"Error ejecutando {nombre}: {e}", exc_info=True)
        return {"exito": False, "error": f"Error interno al ejecutar {nombre}: {str(e)}"}


# ==================== CONFIRMAR RECORDATORIO ====================
async def confirmar_recordatorio(chat_id: int, datos: Dict) -> str:
    """Inserta el recordatorio confirmado en la base de datos."""
    try:
        mensaje = datos["mensaje"]
        fecha_utc_str = datos["fecha_utc"]
        fecha_utc = datetime.fromisoformat(fecha_utc_str)

        await ejecutar_query(
            "INSERT INTO recordatorios (chat_id, mensaje, fecha_recordatorio) VALUES ($1, $2, $3)",
            (chat_id, mensaje, fecha_utc),
        )

        fecha_local = utc_a_local(fecha_utc)
        fecha_mostrar = fecha_local.strftime("%d/%m/%Y %I:%M %p")
        return f"✅ Recordatorio programado para el {fecha_mostrar}.\n\n📝 {mensaje}"
    except Exception as e:
        logger.error(f"Error confirmando recordatorio: {e}", exc_info=True)
        return "⚠️ Error al guardar el recordatorio. Intenta de nuevo."


# ==================== PROCESAMIENTO DE MENSAJES ====================
async def procesar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    # Transcribir voz o tomar texto
    texto = None
    if update.message and update.message.voice:
        await update.message.reply_chat_action("typing")
        texto = await transcribir_audio(update.message.voice)
        if texto:
            await update.message.reply_text(f"📝 \"{texto}\"")
        else:
            await update.message.reply_text("⚠️ No pude entender el audio, jefe. ¿Puedes repetirlo por texto?")
            return
    elif update.message and update.message.text:
        texto = update.message.text.strip()
    else:
        return

    if not texto:
        return

    # Obtener historial
    historial = await obtener_historial(chat_id)
    user_msg = {"role": "user", "content": texto}
    historial.append(user_msg)
    await guardar_historial(chat_id, user_msg)

    # Detectar confirmación de recordatorio pendiente
    datos_pendientes = context.user_data.get("recordatorio_pendiente")
    if datos_pendientes:
        texto_lower = texto.lower().strip()
        if texto_lower in ("sí", "si", "yes", "confirmo", "confirmo.", "confirmar"):
            resultado_texto = await confirmar_recordatorio(chat_id, datos_pendientes)
            context.user_data.pop("recordatorio_pendiente", None)
            assistant_msg = {"role": "assistant", "content": resultado_texto}
            await guardar_historial(chat_id, assistant_msg)
            await update.message.reply_text(resultado_texto)
            return
        elif texto_lower in ("no", "cancelar", "olvidalo", "olvídalo", "nop"):
            context.user_data.pop("recordatorio_pendiente", None)
            assistant_msg = {"role": "assistant", "content": "❌ Recordatorio cancelado."}
            await guardar_historial(chat_id, assistant_msg)
            await update.message.reply_text("❌ Recordatorio cancelado.")
            return
        elif texto_lower.startswith("mañana"):
            # Reprogramar para mañana
            import copy
            datos_mañana = copy.deepcopy(datos_pendientes)
            fecha_local = datetime.fromisoformat(datos_mañana["fecha_local"])
            fecha_mañana = fecha_local + timedelta(days=1)
            fecha_utc_mañana = local_a_utc(fecha_mañana)
            datos_mañana["fecha_local"] = fecha_mañana.isoformat()
            datos_mañana["fecha_utc"] = fecha_utc_mañana.isoformat()
            resultado_texto = await confirmar_recordatorio(chat_id, datos_mañana)
            context.user_data.pop("recordatorio_pendiente", None)
            assistant_msg = {"role": "assistant", "content": resultado_texto}
            await guardar_historial(chat_id, assistant_msg)
            await update.message.reply_text(resultado_texto)
            return

    # Llamar al LLM
    respuesta_llm = await llamar_llm(chat_id, historial)

    # Guardar respuesta del asistente
    await guardar_historial(chat_id, respuesta_llm)

    # Si hay tool_calls, ejecutarlas
    if respuesta_llm.get("tool_calls"):
        tool_results = []
        for tc in respuesta_llm["tool_calls"]:
            resultado = await ejecutar_tool_call(tc, chat_id)
            tool_msg = {
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": json.dumps(resultado, ensure_ascii=False, default=str),
            }
            tool_results.append(tool_msg)
            await guardar_historial(chat_id, tool_msg)

            # Si la tool requiere confirmación de recordatorio
            if resultado.get("requiere_confirmacion") and resultado.get("datos_originales"):
                context.user_data["recordatorio_pendiente"] = resultado["datos_originales"]
                await update.message.reply_text(resultado["error"])
                return

        # Segunda llamada al LLM con los resultados de las tools
        historial = await obtener_historial(chat_id)
        segunda_respuesta = await llamar_llm(chat_id, historial)
        await guardar_historial(chat_id, segunda_respuesta)

        texto_respuesta = segunda_respuesta.get("content", "")
        if texto_respuesta:
            await update.message.reply_text(texto_respuesta)
        return

    # Respuesta directa sin tools
    texto_respuesta = respuesta_llm.get("content", "")
    if texto_respuesta:
        await update.message.reply_text(texto_respuesta)


# ==================== COMANDOS ====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔨 *Asistente Taller de Aluminio*\n\n"
        "Envíame un mensaje de voz o texto para:\n"
        "• Registrar proyectos y clientes\n"
        "• Registrar pagos y anticipos\n"
        "• Consultar proyectos activos/liquidados\n"
        "• Marcar presupuestos enviados\n"
        "• Marcar material comprado\n"
        "• Programar recordatorios\n"
        "• Registrar gastos generales\n\n"
        "¿En qué te ayudo, jefe?",
        parse_mode="Markdown",
    )


async def cmd_limpiar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await limpiar_historial(chat_id)
    context.user_data.clear()
    await update.message.reply_text("🧹 Historial limpiado.")


async def cmd_recordatorios(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    resultado = await tool_consultar_recordatorios(chat_id)
    if not resultado.get("data"):
        await update.message.reply_text(resultado.get("mensaje", "No hay recordatorios."))
        return
    lineas = ["⏰ Recordatorios pendientes:"]
    for r in resultado["data"]:
        lineas.append(f"  • ID {r['id_recordatorio']}: {r['mensaje']} — {r['fecha_local']}")
    await update.message.reply_text("\n".join(lineas))


# ==================== REVISIÓN DE RECORDATORIOS ====================
async def revisar_recordatorios(context: ContextTypes.DEFAULT_TYPE):
    """Job periódico que envía recordatorios vencidos."""
    ahora = datetime.utcnow()
    query = """
        SELECT id, chat_id, mensaje
        FROM recordatorios
        WHERE enviado = FALSE AND fecha_recordatorio <= $1
        ORDER BY fecha_recordatorio ASC
        LIMIT 10
    """
    resultados = await ejecutar_query(query, (ahora,), fetch=True)
    for r in resultados:
        try:
            await context.bot.send_message(
                chat_id=r["chat_id"],
                text=f"🔔 RECORDATORIO:\n{r['mensaje']}",
            )
            await ejecutar_query(
                "UPDATE recordatorios SET enviado = TRUE WHERE id = $1", (r["id"],)
            )
        except Exception as e:
            logger.error(f"Error enviando recordatorio {r['id']}: {e}")


# ==================== STARTUP / SHUTDOWN ====================
async def post_init(application):
    await init_db_pool()
    await crear_tablas()
    # Revisar recordatorios cada 30 segundos
    application.job_queue.run_repeating(
        revisar_recordatorios, interval=30, first=5, name="revisar_recordatorios"
    )
    logger.info("🚀 Bot iniciado correctamente.")


async def post_shutdown(application):
    if _db_pool:
        await _db_pool.close()
        logger.info("🔴 Pool de conexiones cerrado.")


# ==================== MAIN ====================
def main():
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("limpiar", cmd_limpiar))
    app.add_handler(CommandHandler("recordatorios", cmd_recordatorios))
    app.add_handler(
        MessageHandler(
            filters.VOICE | filters.TEXT & ~filters.COMMAND, procesar_mensaje
        )
    )

    logger.info("Iniciando bot...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
