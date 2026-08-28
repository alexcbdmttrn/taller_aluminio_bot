import os
import json
import re
import logging
import asyncio
import io
import tempfile
from decimal import Decimal
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
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


# ==================== POOL DE CONEXIONES ASYNCPG ====================
_db_pool = None


async def init_db_pool():
    """Inicializa el pool de conexiones asyncpg."""
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
    """Obtiene una conexión del pool."""
    return await _db_pool.acquire()


async def put_connection(conn):
    """Devuelve la conexión al pool."""
    await _db_pool.release(conn)


# ==================== FUNCIONES DE BASE DE DATOS (ASÍNCRONAS) ====================

async def ejecutar_query(query, params=None, fetch=False):
    """Ejecuta una consulta SQL usando el pool asyncpg."""
    conn = await get_connection()
    try:
        if fetch:
            return await conn.fetch(query, *(params or ()))
        else:
            return await conn.execute(query, *(params or ()))
    finally:
        await put_connection(conn)


async def buscar_o_crear_cliente(nombre_cliente, telefono=None, direccion=None, notas=None):
    """Busca o crea un cliente, devuelve su ID."""
    nombre_cliente = nombre_cliente.lower().strip()
    query = "SELECT id FROM clientes WHERE unaccent(nombre) ILIKE unaccent($1)"
    result = await ejecutar_query(query, (f"%{nombre_cliente}%",), fetch=True)
    if result:
        cliente_id = result[0]["id"]
        if telefono or direccion or notas:
            updates = []
            params = []
            if telefono:
                updates.append("telefono = $1")
                params.append(telefono)
            if direccion:
                updates.append("direccion = $2")
                params.append(direccion)
            if notas:
                updates.append("notas_adicionales = $3")
                params.append(notas)
            if updates:
                params.append(cliente_id)
                # Reconstruir la consulta de actualización
                set_clause = ", ".join(updates)
                # Ajustar índices de parámetros
                param_placeholders = []
                for i, _ in enumerate(params[:-1], 1):
                    param_placeholders.append(f"${i}")
                set_clause_final = ", ".join(
                    [f"{col} = ${i+1}" for i, col in enumerate(updates)]
                )
                await ejecutar_query(
                    f"UPDATE clientes SET {set_clause_final} WHERE id = ${len(params)}",
                    params,
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


# ==================== HERRAMIENTAS (TOOLS) ASÍNCRONAS ====================

async def tool_registrar_proyecto(
    cliente: str,
    nombre_corto: str,
    descripcion: str,
    monto: float,
    telefono: str = None,
    direccion: str = None,
    notas: str = None,
):
    """Registra un nuevo proyecto para un cliente."""
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
    proy_id = result[0]["id"]
    return {
        "exito": True,
        "mensaje": f"Proyecto '{nombre_corto}' registrado para {cliente}. ID: {proy_id}",
    }


async def tool_registrar_pago(cliente: str, monto: float, referencia: str = None):
    """Registra un pago/anticipo para el proyecto más reciente activo del cliente."""
    proyectos = await obtener_proyectos_activos(cliente)
    if not proyectos:
        return {"exito": False, "error": f"No hay proyectos activos para {cliente}."}
    pid, nc, desc, total, pagado, estado, mat_comp, costo_mat, presup, cliente_nombre = proyectos[0]
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
    """Marca el presupuesto como enviado para el proyecto del cliente."""
    proyectos = await obtener_proyectos_activos(cliente)
    if not proyectos:
        return {"exito": False, "error": f"No hay proyectos activos para {cliente}."}
    if nombre_corto:
        for p in proyectos:
            if nombre_corto.lower() in p[1].lower():
                pid, nc, desc, total, pagado, estado, mat_comp, costo_mat, presup, cliente_nombre = p
                break
        else:
            return {
                "exito": False,
                "error": f"No encontré proyecto '{nombre_corto}' para {cliente}.",
            }
    else:
        pid, nc, desc, total, pagado, estado, mat_comp, costo_mat, presup, cliente_nombre = proyectos[0]

    updates = [
        "presupuesto_enviado = TRUE",
        "fecha_presupuesto = CURRENT_TIMESTAMP",
        "estado = 'Presupuesto enviado'",
    ]
    params = []
    if monto is not None and monto > 0:
        updates.append("monto_total = $1")
        params.append(monto)
    if descripcion:
        updates.append("descripcion = COALESCE($2, descripcion)")
        params.append(descripcion)
    params.append(pid)
    await ejecutar_query(
        f"UPDATE proyectos SET {', '.join(updates)} WHERE id = ${len(params)}", params
    )
    return {
        "exito": True,
        "mensaje": f"Presupuesto marcado como enviado para '{nc}' de {cliente}.",
    }


async def tool_consultar_proyectos(tipo: str = "activos", cliente: str = None):
    """Consulta proyectos según tipo (activos, liquidados, cancelados, deudores). LIMIT 5."""
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
                "presupuesto_enviado": row["presupuesto_enviado"],
                "telefono": row["telefono"],
                "direccion": row["direccion"],
            }
        )
    return {"exito": True, "data": data}


async def tool_cerrar_proyecto(cliente: str, nombre_corto: str = None):
    """Liquida un proyecto si el saldo es cero; si no, indica el monto pendiente."""
    proyectos = await obtener_proyectos_activos(cliente)
    if not proyectos:
        return {"exito": False, "error": f"No hay proyectos activos para {cliente}."}
    if nombre_corto:
        for p in proyectos:
            if nombre_corto.lower() in p[1].lower():
                pid, nc, desc, total, pagado, estado, mat_comp, costo_mat, presup, cliente_nombre = p
                break
        else:
            return {
                "exito": False,
                "error": f"No encontré proyecto '{nombre_corto}' para {cliente}.",
            }
    else:
        pid, nc, desc, total, pagado, estado, mat_comp, costo_mat, presup, cliente_nombre = proyectos[0]

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
    """Cancela un proyecto (cambia estado a Cancelado)."""
    query = """
        SELECT p.id, p.nombre_corto
        FROM proyectos p JOIN clientes c ON p.cliente_id = c.id
        WHERE unaccent(c.nombre) ILIKE unaccent($1) AND p.estado NOT IN ('Liquidado', 'Cancelado')
        ORDER BY p.fecha_creacion DESC
    """
    proyectos = await ejecutar_query(query, (f"%{cliente}%",), fetch=True)
    if not proyectos:
        return {"exito": False, "error": f"No hay proyectos activos para {cliente}."}
    if nombre_corto:
        for p in proyectos:
            if nombre_corto.lower() in p[1].lower():
                pid, nc = p["id"], p["nombre_corto"]
                break
        else:
            return {
                "exito": False,
                "error": f"No encontré proyecto '{nombre_corto}' para {cliente}.",
            }
    else:
        pid, nc = proyectos[0]["id"], proyectos[0]["nombre_corto"]
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
    """Borra físicamente un proyecto. Solo si confirmado es True."""
    if not confirmado:
        return {
            "exito": False,
            "error": "Se requiere confirmación explícita para borrar. Pregunta al usuario: '¿Estás seguro de borrar el proyecto?'.",
        }
    query = """
        SELECT p.id, p.nombre_corto
        FROM proyectos p JOIN clientes c ON p.cliente_id = c.id
        WHERE unaccent(c.nombre) ILIKE unaccent($1) AND p.estado NOT IN ('Liquidado', 'Cancelado')
        ORDER BY p.fecha_creacion DESC
    """
    proyectos = await ejecutar_query(query, (f"%{cliente}%",), fetch=True)
    if not proyectos:
        return {"exito": False, "error": f"No hay proyectos activos para {cliente}."}
    if nombre_corto:
        for p in proyectos:
            if nombre_corto.lower() in p[1].lower():
                pid, nc = p["id"], p["nombre_corto"]
                break
        else:
            return {
                "exito": False,
                "error": f"No encontré proyecto '{nombre_corto}' para {cliente}.",
            }
    else:
        pid, nc = proyectos[0]["id"], proyectos[0]["nombre_corto"]
    await ejecutar_query("DELETE FROM proyectos WHERE id = $1", (pid,))
    await ejecutar_query(
        "DELETE FROM clientes WHERE id NOT IN (SELECT DISTINCT cliente_id FROM proyectos WHERE cliente_id IS NOT NULL)"
    )
    return {
        "exito": True,
        "mensaje": f"Proyecto '{nc}' de {cliente} eliminado definitivamente.",
    }


async def tool_registrar_gasto(descripcion: str, monto: float):
    """Registra un gasto general."""
    await ejecutar_query(
        "INSERT INTO gastos (descripcion, monto) VALUES ($1, $2)",
        (descripcion, monto),
    )
    return {
        "exito": True,
        "mensaje": f"Gasto '{descripcion}' de ${monto:.2f} registrado.",
    }


async def tool_consultar_gastos(limite: int = 10):
    """Consulta los últimos gastos."""
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
    """Explica por qué un proyecto está en su estado actual."""
    query = """
        SELECT p.id, p.nombre_corto, p.monto_total, p.monto_pagado, p.estado
        FROM proyectos p JOIN clientes c ON p.cliente_id = c.id
        WHERE unaccent(c.nombre) ILIKE unaccent($1) AND p.estado NOT IN ('Liquidado', 'Cancelado')
        ORDER BY p.fecha_creacion DESC
    """
    proyectos = await ejecutar_query(query, (f"%{cliente}%",), fetch=True)
    if not proyectos:
        return {"exito": False, "error": f"No hay proyectos activos para {cliente}."}
    if nombre_corto:
        for p in proyectos:
            if nombre_corto.lower() in p[1].lower():
                pid, nc, total, pagado, estado = (
                    p["id"],
                    p["nombre_corto"],
                    p["monto_total"],
                    p["monto_pagado"],
                    p["estado"],
                )
                break
        else:
            return {
                "exito": False,
                "error": f"No encontré proyecto '{nombre_corto}' para {cliente}.",
            }
    else:
        pid, nc, total, pagado, estado = (
            proyectos[0]["id"],
            proyectos[0]["nombre_corto"],
            proyectos[0]["monto_total"],
            proyectos[0]["monto_pagado"],
            proyectos[0]["estado"],
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
                    "nombre_corto": {
                        "type": "string",
                        "description": "Nombre corto del proyecto",
                    },
                    "descripcion": {"type": "string", "description": "Descripción del trabajo"},
                    "monto": {"type": "number", "description": "Presupuesto total del proyecto"},
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
            "description": "Registra un pago o anticipo de un cliente. Siempre pregunta el monto y a qué proyecto aplica si hay varios.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cliente": {"type": "string", "description": "Nombre del cliente"},
                    "monto": {"type": "number", "description": "Monto del pago"},
                    "referencia": {
                        "type": "string",
                        "description": "Concepto del pago (opcional)",
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
            "description": "Marca un proyecto como 'Presupuesto enviado'. Opcionalmente actualiza monto y descripción.",
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
            "name": "tool_consultar_proyectos",
            "description": "Consulta proyectos según tipo (activos, liquidados, cancelados, deudores). Devuelve un máximo de 5 resultados para no saturar. Si no encuentras el proyecto buscado en la lista devuelta, pídele al usuario el nombre exacto del cliente para filtrar mejor.",
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
            "description": "Liquida un proyecto si el saldo es cero. Si tiene saldo pendiente, indicará el monto y se debe registrar pago primero.",
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
            "description": "Cancela un proyecto (cambia estado a Cancelado). No borra datos.",
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
            "description": "BORRA FÍSICAMENTE un proyecto. Solo ejecutar después de confirmación explícita del usuario.",
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
                    "descripcion": {"type": "string", "description": "Descripción del gasto"},
                    "monto": {"type": "number", "description": "Monto del gasto"},
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
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_explicar_estado",
            "description": "Explica por qué un proyecto está en su estado actual.",
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
}

# ==================== PROMPT DEL SISTEMA (BASE) ====================
SYSTEM_PROMPT_BASE = (
    "Eres el asistente de gestión de proyectos de un taller de aluminio. "
    "Tu tarea es ayudar a registrar datos, consultar materiales y administrar pagos. "
    "No asumas información. Si el usuario te pide registrar un gasto o un pago, pero falta la cantidad, el concepto o el proyecto, PREGÚNTALE en lenguaje natural antes de ejecutar la herramienta. "
    "Solo ejecuta herramientas de base de datos cuando tengas toda la información requerida explícita en la conversación. "
    "Habla de forma directa y clara, usando 'jefe' o 'patrón' ocasionalmente. "
    "Cuando muestres listas, preséntalas de manera ordenada, con emojis para facilitar la lectura. "
    "Si el usuario pide borrar algo, siempre pregunta confirmación primero, y solo ejecuta la herramienta cuando el usuario confirme explícitamente."
)

# ==================== FUNCIÓN DE PODA DE HISTORIAL ====================
def podar_historial(messages: List[Dict]) -> List[Dict]:
    """Mantiene los últimos 12 mensajes, asegurando que las secuencias de tool_calls no se corten."""
    MAX_HISTORIAL = 12
    if len(messages) <= MAX_HISTORIAL:
        return messages

    start = len(messages) - MAX_HISTORIAL
    # Retroceder mientras el mensaje en start sea 'tool' o un 'assistant' con tool_calls huérfanas
    while start > 0:
        msg = messages[start]
        if msg["role"] == "tool":
            start -= 1
            continue
        if msg["role"] == "assistant" and msg.get("tool_calls"):
            # Verificar si los siguientes mensajes cubren todos los tool_calls
            tool_call_ids = [tc["id"] for tc in msg["tool_calls"]]
            next_msgs = messages[start + 1 : start + len(tool_call_ids) + 1]
            found_ids = [m["tool_call_id"] for m in next_msgs if m["role"] == "tool"]
            if not all(tid in found_ids for tid in tool_call_ids):
                start -= 1
                continue
        break

    start = max(0, start)
    return messages[start:]


# ==================== MANEJO DE AUDIO ====================
def transcribir_audio_buffer(buffer: io.BytesIO) -> str:
    """Transcribe un buffer de audio usando Groq (síncrono, se ejecuta en hilo)."""
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


# ==================== BUCLE PRINCIPAL DEL AGENTE (ASÍNCRONO) ====================
async def procesar_mensaje(
    update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str
):
    """Procesa el mensaje del usuario usando el agente con tools (totalmente asíncrono)."""
    if not texto:
        await update.message.reply_text("No entendí el mensaje. ¿Puedes repetirlo?")
        return

    # Inicializar historial si no existe
    if "messages" not in context.user_data:
        context.user_data["messages"] = []

    # Agregar mensaje del usuario al historial
    context.user_data["messages"].append({"role": "user", "content": texto})

    # Poda del historial (antes de enviar a la API)
    historial_podado = podar_historial(context.user_data["messages"])

    # Inyectar mensaje de sistema con fecha/hora actual (dinámico)
    fecha_actual = ahora_cdmx().strftime("%Y-%m-%d %H:%M")
    system_msg = {
        "role": "system",
        "content": f"La fecha y hora actual en México es: {fecha_actual}. {SYSTEM_PROMPT_BASE}",
    }
    # Construir mensajes para la API: system + historial_podado
    mensajes_api = [system_msg] + historial_podado

    max_iteraciones = 5
    for _ in range(max_iteraciones):
        try:
            # Llamada asíncrona a DeepSeek
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
            await update.message.reply_text(
                "❌ Error al comunicarme con el asistente. Intenta de nuevo."
            )
            return

        message = response.choices[0].message

        # Si no hay tool calls, responder y terminar
        if not message.tool_calls:
            respuesta = message.content
            if respuesta:
                context.user_data["messages"].append(
                    {"role": "assistant", "content": respuesta}
                )
                await update.message.reply_text(respuesta, parse_mode="Markdown")
            else:
                await update.message.reply_text("No tengo respuesta para eso.")
            return

        # Procesar tool calls
        tool_calls = message.tool_calls
        # Agregar el mensaje del asistente con tool_calls al historial
        context.user_data["messages"].append(message.model_dump())

        for tool_call in tool_calls:
            function_name = tool_call.function.name
            function_args = json.loads(tool_call.function.arguments)

            tool_func = TOOL_FUNCTIONS.get(function_name)
            if not tool_func:
                result = {"error": f"Tool '{function_name}' no encontrada."}
            else:
                try:
                    # Ejecutar la función tool (asíncrona)
                    result = await tool_func(**function_args)
                except Exception as e:
                    logger.error(f"Error ejecutando tool {function_name}: {e}")
                    result = {"error": str(e)}

            # Agregar el resultado de la tool al historial
            context.user_data["messages"].append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(result),
                }
            )

        # Después de agregar los mensajes tool, reiniciamos el loop
        historial_podado = podar_historial(context.user_data["messages"])
        mensajes_api = [system_msg] + historial_podado

    await update.message.reply_text(
        "El proceso ha tomado demasiados pasos. Por favor, simplifica tu solicitud."
    )


# ==================== MANEJADORES DE TELEGRAM ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start: resetea el historial y da la bienvenida."""
    context.user_data["messages"] = []
    await update.message.reply_text(
        "¡Hola, jefe! 🛠️ Soy su asistente de gestión de proyectos.\n\n"
        "Puedo ayudarle a:\n"
        "- Registrar clientes y proyectos\n"
        "- Registrar pagos y anticipos\n"
        "- Consultar proyectos activos, liquidados, cancelados\n"
        "- Marcar presupuestos como enviados\n"
        "- Registrar gastos generales\n"
        "- Cancelar o borrar proyectos (con confirmación)\n\n"
        "Simplemente hable conmigo en lenguaje natural. ¿En qué le ayudo?"
    )


async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manejador principal para mensajes de texto y voz (asíncrono)."""
    if update.message.text:
        texto = update.message.text
        await procesar_mensaje(update, context, texto)
    elif update.message.voice:
        if not groq_client:
            await update.message.reply_text(
                "❌ El servicio de transcripción de voz no está configurado."
            )
            return
        try:
            await update.message.reply_text("🎙️ Escuchando...")
            voice_file = await update.message.voice.get_file()
            buffer = io.BytesIO()
            await voice_file.download_to_memory(buffer)
            # Transcribir en hilo separado (bloqueante)
            texto = await asyncio.to_thread(transcribir_audio_buffer, buffer)
            if not texto:
                await update.message.reply_text(
                    "❌ No pude entender el audio. ¿Puedes repetirlo o escribirlo?"
                )
                return
            await update.message.reply_text(
                f"📝 *\"{texto}\"*", parse_mode="Markdown"
            )
            await procesar_mensaje(update, context, texto)
        except Exception as e:
            logger.error(f"Error manejando audio: {e}")
            await update.message.reply_text(
                "❌ Error al procesar el audio. Intenta de nuevo."
            )


# ==================== INICIO ====================
async def main():
    """Función principal asíncrona."""
    # Inicializar pool de base de datos
    await init_db_pool()

    # Configurar persistencia de memoria
    persistence = PicklePersistence(filepath="bot_data.pickle")

    # Crear aplicación con persistencia
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .persistence(persistence)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handler))
    app.add_handler(MessageHandler(filters.VOICE, handler))

    logger.info("🤖 Bot asíncrono con asyncpg, poda de historial y persistencia iniciado.")
    await app.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
