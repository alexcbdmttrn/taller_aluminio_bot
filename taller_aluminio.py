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
import psycopg2
from psycopg2 import pool
from openai import AsyncOpenAI
from groq import Groq
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# ==================== CONFIGURACIÓN ====================
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
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
    ZONA_HORARIA = pytz.timezone('America/Mexico_City')
except Exception:
    ZONA_HORARIA = None

def ahora_cdmx():
    if ZONA_HORARIA:
        return datetime.now(ZONA_HORARIA)
    return datetime.utcnow() - timedelta(hours=6)

# ==================== POOL DE CONEXIONES POSTGRESQL ====================
try:
    db_pool = pool.SimpleConnectionPool(1, 20, DATABASE_URL)
    logger.info("✅ Pool de conexiones PostgreSQL iniciado.")
except Exception as e:
    logger.error(f"❌ Error al crear pool de conexiones: {e}")
    exit(1)

def get_connection():
    return db_pool.getconn()

def put_connection(conn):
    db_pool.putconn(conn)

# ==================== FUNCIONES DE BASE DE DATOS (SÍNCRONAS, PARA EJECUTAR EN HILOS) ====================

def ejecutar_query_sync(query, params=None, fetch=False):
    """Ejecuta una consulta SQL de forma síncrona (para ser llamada con asyncio.to_thread)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(query, params or ())
            if fetch:
                return cur.fetchall()
            conn.commit()
            return cur.rowcount
    finally:
        put_connection(conn)

# ==================== HERRAMIENTAS (TOOLS) ====================
# Todas las herramientas son síncronas, se ejecutarán en hilos separados.

def tool_registrar_proyecto(cliente: str, nombre_corto: str, descripcion: str, monto: float,
                            telefono: str = None, direccion: str = None, notas: str = None):
    """Registra un nuevo proyecto para un cliente."""
    # Buscar o crear cliente
    nombre_cliente = cliente.lower().strip()
    query = "SELECT id FROM clientes WHERE unaccent(nombre) ILIKE unaccent(%s)"
    result = ejecutar_query_sync(query, (f"%{nombre_cliente}%",), fetch=True)
    if result:
        cliente_id = result[0][0]
        if telefono or direccion or notas:
            updates = []
            params = []
            if telefono:
                updates.append("telefono = %s")
                params.append(telefono)
            if direccion:
                updates.append("direccion = %s")
                params.append(direccion)
            if notas:
                updates.append("notas_adicionales = %s")
                params.append(notas)
            if updates:
                params.append(cliente_id)
                ejecutar_query_sync(f"UPDATE clientes SET {', '.join(updates)} WHERE id = %s", params)
    else:
        insert = "INSERT INTO clientes (nombre, telefono, direccion, notas_adicionales) VALUES (%s, %s, %s, %s) RETURNING id"
        result = ejecutar_query_sync(insert, (nombre_cliente, telefono, direccion, notas), fetch=True)
        cliente_id = result[0][0]
    # Insertar proyecto
    insert_proy = """
        INSERT INTO proyectos (cliente_id, nombre_corto, descripcion, monto_total, monto_pagado, estado, notas_adicionales)
        VALUES (%s, %s, %s, %s, 0, 'Pendiente de cotizar', %s) RETURNING id
    """
    result = ejecutar_query_sync(insert_proy, (cliente_id, nombre_corto or 'Proyecto General', descripcion, monto, notas), fetch=True)
    proy_id = result[0][0]
    return {"exito": True, "mensaje": f"Proyecto '{nombre_corto}' registrado para {cliente}. ID: {proy_id}"}

def tool_registrar_pago(cliente: str, monto: float, referencia: str = None):
    """Registra un pago/anticipo para el proyecto más reciente activo del cliente."""
    query = """
        SELECT p.id, p.nombre_corto, p.monto_total, p.monto_pagado, p.estado
        FROM proyectos p JOIN clientes c ON p.cliente_id = c.id
        WHERE unaccent(c.nombre) ILIKE unaccent(%s) AND p.estado NOT IN ('Liquidado', 'Cancelado')
        ORDER BY p.fecha_creacion DESC LIMIT 1
    """
    proyectos = ejecutar_query_sync(query, (f"%{cliente}%",), fetch=True)
    if not proyectos:
        return {"exito": False, "error": f"No hay proyectos activos para {cliente}."}
    pid, nc, total, pagado, estado = proyectos[0]
    nuevo_pagado = Decimal(str(pagado)) + Decimal(str(monto))
    saldo = max(Decimal('0'), Decimal(str(total)) - nuevo_pagado)
    nuevo_estado = "Liquidado" if saldo == 0 else "Por cobrar"
    if monto > 0 and estado == "Pendiente de cotizar":
        nuevo_estado = "En proceso"
    ejecutar_query_sync(
        "UPDATE proyectos SET monto_pagado = %s, estado = %s WHERE id = %s",
        (float(nuevo_pagado), nuevo_estado, pid)
    )
    return {
        "exito": True,
        "mensaje": f"Pago de ${monto:.2f} registrado para '{nc}' de {cliente}. Saldo restante: ${float(saldo):.2f}. Estado: {nuevo_estado}"
    }

def tool_marcar_presupuesto_enviado(cliente: str, nombre_corto: str = None, monto: float = None, descripcion: str = None):
    """Marca el presupuesto como enviado para el proyecto del cliente."""
    # Obtener proyectos activos del cliente
    query = """
        SELECT p.id, p.nombre_corto, p.descripcion, p.monto_total, p.estado
        FROM proyectos p JOIN clientes c ON p.cliente_id = c.id
        WHERE unaccent(c.nombre) ILIKE unaccent(%s) AND p.estado NOT IN ('Liquidado', 'Cancelado')
        ORDER BY p.fecha_creacion DESC
    """
    proyectos = ejecutar_query_sync(query, (f"%{cliente}%",), fetch=True)
    if not proyectos:
        return {"exito": False, "error": f"No hay proyectos activos para {cliente}."}
    if nombre_corto:
        for p in proyectos:
            if nombre_corto.lower() in p[1].lower():
                pid, nc, desc, total, estado = p
                break
        else:
            return {"exito": False, "error": f"No encontré proyecto '{nombre_corto}' para {cliente}."}
    else:
        pid, nc, desc, total, estado = proyectos[0]
    updates = ["presupuesto_enviado = TRUE", "fecha_presupuesto = CURRENT_TIMESTAMP", "estado = 'Presupuesto enviado'"]
    params = []
    if monto is not None and monto > 0:
        updates.append("monto_total = %s")
        params.append(monto)
    if descripcion:
        updates.append("descripcion = COALESCE(%s, descripcion)")
        params.append(descripcion)
    params.append(pid)
    ejecutar_query_sync(f"UPDATE proyectos SET {', '.join(updates)} WHERE id = %s", params)
    return {"exito": True, "mensaje": f"Presupuesto marcado como enviado para '{nc}' de {cliente}."}

def tool_consultar_proyectos(tipo: str = "activos", cliente: str = None):
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
            WHERE unaccent(c.nombre) ILIKE unaccent(%s) AND {condicion}
            ORDER BY p.fecha_creacion DESC
            LIMIT 5
        """
        resultados = ejecutar_query_sync(query, (f"%{cliente}%",), fetch=True)
    else:
        query = f"""
            SELECT c.nombre, p.nombre_corto, p.descripcion, p.monto_total, p.monto_pagado, p.estado,
                   p.material_comprado, p.presupuesto_enviado, c.telefono, c.direccion
            FROM proyectos p JOIN clientes c ON p.cliente_id = c.id
            WHERE {condicion}
            ORDER BY c.nombre, p.fecha_creacion DESC
            LIMIT 5
        """
        resultados = ejecutar_query_sync(query, fetch=True)
    
    if not resultados:
        return {"exito": True, "mensaje": "No hay proyectos en este estado.", "data": []}
    
    data = []
    for row in resultados:
        data.append({
            "cliente": row[0],
            "proyecto": row[1] or "General",
            "descripcion": row[2],
            "total": float(row[3]),
            "pagado": float(row[4]),
            "saldo": float(row[3]) - float(row[4]),
            "estado": row[5],
            "material_comprado": row[6],
            "presupuesto_enviado": row[7],
            "telefono": row[8],
            "direccion": row[9]
        })
    return {"exito": True, "data": data}

def tool_cerrar_proyecto(cliente: str, nombre_corto: str = None):
    """Liquida un proyecto si el saldo es cero; si no, indica el monto pendiente."""
    query = """
        SELECT p.id, p.nombre_corto, p.monto_total, p.monto_pagado, p.estado
        FROM proyectos p JOIN clientes c ON p.cliente_id = c.id
        WHERE unaccent(c.nombre) ILIKE unaccent(%s) AND p.estado NOT IN ('Liquidado', 'Cancelado')
        ORDER BY p.fecha_creacion DESC
    """
    proyectos = ejecutar_query_sync(query, (f"%{cliente}%",), fetch=True)
    if not proyectos:
        return {"exito": False, "error": f"No hay proyectos activos para {cliente}."}
    if nombre_corto:
        for p in proyectos:
            if nombre_corto.lower() in p[1].lower():
                pid, nc, total, pagado, estado = p
                break
        else:
            return {"exito": False, "error": f"No encontré proyecto '{nombre_corto}' para {cliente}."}
    else:
        pid, nc, total, pagado, estado = proyectos[0]
    saldo = total - pagado
    if saldo <= 0:
        ejecutar_query_sync("UPDATE proyectos SET estado = 'Liquidado' WHERE id = %s", (pid,))
        return {"exito": True, "mensaje": f"Proyecto '{nc}' de {cliente} liquidado (saldo $0)."}
    else:
        return {"exito": False, "error": f"El proyecto '{nc}' tiene saldo pendiente de ${saldo:.2f}. Primero registra el pago restante."}

def tool_cancelar_proyecto(cliente: str, nombre_corto: str = None):
    """Cancela un proyecto (cambia estado a Cancelado)."""
    query = """
        SELECT p.id, p.nombre_corto
        FROM proyectos p JOIN clientes c ON p.cliente_id = c.id
        WHERE unaccent(c.nombre) ILIKE unaccent(%s) AND p.estado NOT IN ('Liquidado', 'Cancelado')
        ORDER BY p.fecha_creacion DESC
    """
    proyectos = ejecutar_query_sync(query, (f"%{cliente}%",), fetch=True)
    if not proyectos:
        return {"exito": False, "error": f"No hay proyectos activos para {cliente}."}
    if nombre_corto:
        for p in proyectos:
            if nombre_corto.lower() in p[1].lower():
                pid, nc = p
                break
        else:
            return {"exito": False, "error": f"No encontré proyecto '{nombre_corto}' para {cliente}."}
    else:
        pid, nc = proyectos[0]
    ejecutar_query_sync("UPDATE proyectos SET estado = 'Cancelado' WHERE id = %s", (pid,))
    return {"exito": True, "mensaje": f"Proyecto '{nc}' de {cliente} ha sido cancelado."}

def tool_borrar_proyecto(cliente: str, nombre_corto: str = None, confirmado: bool = False):
    """Borra físicamente un proyecto. Solo si confirmado es True."""
    if not confirmado:
        return {"exito": False, "error": "Se requiere confirmación explícita para borrar. Pregunta al usuario: '¿Estás seguro de borrar el proyecto?'."}
    query = """
        SELECT p.id, p.nombre_corto
        FROM proyectos p JOIN clientes c ON p.cliente_id = c.id
        WHERE unaccent(c.nombre) ILIKE unaccent(%s) AND p.estado NOT IN ('Liquidado', 'Cancelado')
        ORDER BY p.fecha_creacion DESC
    """
    proyectos = ejecutar_query_sync(query, (f"%{cliente}%",), fetch=True)
    if not proyectos:
        return {"exito": False, "error": f"No hay proyectos activos para {cliente}."}
    if nombre_corto:
        for p in proyectos:
            if nombre_corto.lower() in p[1].lower():
                pid, nc = p
                break
        else:
            return {"exito": False, "error": f"No encontré proyecto '{nombre_corto}' para {cliente}."}
    else:
        pid, nc = proyectos[0]
    ejecutar_query_sync("DELETE FROM proyectos WHERE id = %s", (pid,))
    ejecutar_query_sync("DELETE FROM clientes WHERE id NOT IN (SELECT DISTINCT cliente_id FROM proyectos WHERE cliente_id IS NOT NULL)")
    return {"exito": True, "mensaje": f"Proyecto '{nc}' de {cliente} eliminado definitivamente."}

def tool_registrar_gasto(descripcion: str, monto: float):
    """Registra un gasto general."""
    ejecutar_query_sync("INSERT INTO gastos (descripcion, monto) VALUES (%s, %s)", (descripcion, monto))
    return {"exito": True, "mensaje": f"Gasto '{descripcion}' de ${monto:.2f} registrado."}

def tool_consultar_gastos(limite: int = 10):
    """Consulta los últimos gastos."""
    resultados = ejecutar_query_sync("SELECT fecha, descripcion, monto FROM gastos ORDER BY fecha DESC LIMIT %s", (limite,), fetch=True)
    if not resultados:
        return {"exito": True, "data": [], "mensaje": "No hay gastos registrados."}
    data = [{"fecha": r[0].strftime("%d/%m %H:%M"), "descripcion": r[1], "monto": float(r[2])} for r in resultados]
    return {"exito": True, "data": data}

def tool_explicar_estado(cliente: str, nombre_corto: str = None):
    """Explica por qué un proyecto está en su estado actual."""
    query = """
        SELECT p.id, p.nombre_corto, p.monto_total, p.monto_pagado, p.estado
        FROM proyectos p JOIN clientes c ON p.cliente_id = c.id
        WHERE unaccent(c.nombre) ILIKE unaccent(%s) AND p.estado NOT IN ('Liquidado', 'Cancelado')
        ORDER BY p.fecha_creacion DESC
    """
    proyectos = ejecutar_query_sync(query, (f"%{cliente}%",), fetch=True)
    if not proyectos:
        return {"exito": False, "error": f"No hay proyectos activos para {cliente}."}
    if nombre_corto:
        for p in proyectos:
            if nombre_corto.lower() in p[1].lower():
                pid, nc, total, pagado, estado = p
                break
        else:
            return {"exito": False, "error": f"No encontré proyecto '{nombre_corto}' para {cliente}."}
    else:
        pid, nc, total, pagado, estado = proyectos[0]
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
# (Idéntica a la anterior, pero la descripción de tool_consultar_proyectos incluye el límite de 5)

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
                    "notas": {"type": "string", "description": "Notas adicionales (opcional)"}
                },
                "required": ["cliente", "nombre_corto", "descripcion", "monto"]
            }
        }
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
                    "referencia": {"type": "string", "description": "Concepto del pago (opcional)"}
                },
                "required": ["cliente", "monto"]
            }
        }
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
                    "nombre_corto": {"type": "string", "description": "Nombre corto del proyecto (opcional si solo tiene uno)"},
                    "monto": {"type": "number", "description": "Nuevo monto total (opcional)"},
                    "descripcion": {"type": "string", "description": "Nueva descripción (opcional)"}
                },
                "required": ["cliente"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "tool_consultar_proyectos",
            "description": "Consulta proyectos según tipo (activos, liquidados, cancelados, deudores). Devuelve un máximo de 5 resultados para no saturar. Si no encuentras el proyecto buscado en la lista devuelta, pídele al usuario el nombre exacto del cliente para filtrar mejor.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tipo": {"type": "string", "enum": ["activos", "liquidados", "cancelados", "deudores"], "description": "Tipo de consulta"},
                    "cliente": {"type": "string", "description": "Nombre del cliente (opcional) para filtrar"}
                },
                "required": []
            }
        }
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
                    "nombre_corto": {"type": "string", "description": "Nombre corto del proyecto (opcional si solo tiene uno)"}
                },
                "required": ["cliente"]
            }
        }
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
                    "nombre_corto": {"type": "string", "description": "Nombre corto del proyecto (opcional si solo tiene uno)"}
                },
                "required": ["cliente"]
            }
        }
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
                    "nombre_corto": {"type": "string", "description": "Nombre corto del proyecto (opcional si solo tiene uno)"},
                    "confirmado": {"type": "boolean", "description": "Debe ser true solo si el usuario confirmó explícitamente con 'sí'"}
                },
                "required": ["cliente", "confirmado"]
            }
        }
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
                    "monto": {"type": "number", "description": "Monto del gasto"}
                },
                "required": ["descripcion", "monto"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "tool_consultar_gastos",
            "description": "Muestra los últimos gastos registrados.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limite": {"type": "integer", "description": "Número de gastos a mostrar (por defecto 10)"}
                },
                "required": []
            }
        }
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
                    "nombre_corto": {"type": "string", "description": "Nombre corto del proyecto (opcional si solo tiene uno)"}
                },
                "required": ["cliente"]
            }
        }
    }
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
    """Mantiene los últimos 15 mensajes, asegurando que las secuencias de tool_calls no se corten."""
    MAX_HISTORIAL = 15
    if len(messages) <= MAX_HISTORIAL:
        return messages
    
    # Buscar un punto de corte seguro: comenzar desde el final y retroceder
    # hasta encontrar un mensaje que no sea parte de una secuencia de tool_call
    # que quedaría huérfana.
    # Estrategia: tomar los últimos MAX_HISTORIAL mensajes, pero si el primer mensaje
    # de ese subconjunto es un 'tool' (rol tool) o un 'assistant' con tool_calls que
    # no tiene su tool correspondiente en el subconjunto, retroceder.
    
    # Empezamos con los últimos MAX_HISTORIAL
    trimmed = messages[-MAX_HISTORIAL:]
    
    # Verificar si el primer mensaje es un 'tool' o un 'assistant' con tool_calls
    # que no está acompañado de su mensaje tool.
    # Si el primer mensaje es 'tool', entonces debe haber un 'assistant' anterior con tool_calls.
    # Pero como estamos cortando, debemos asegurarnos de que no haya mensajes 'tool' sin su
    # correspondiente 'assistant' con tool_calls.
    
    # Recorremos desde el inicio del subconjunto hacia adelante:
    # Si encontramos un mensaje con rol 'tool', buscamos hacia atrás (dentro del subconjunto) 
    # el 'assistant' que lo invocó. Si no está, entonces debemos retroceder el corte.
    
    # Simplemente, mientras el primer mensaje sea 'tool', retrocedemos el índice de inicio.
    start = len(messages) - MAX_HISTORIAL
    # Mientras el mensaje en start sea 'tool' y start > 0, retrocedemos
    while start > 0 and messages[start]['role'] == 'tool':
        start -= 1
    # También si es 'assistant' y tiene tool_calls, pero los tools correspondientes no están en el subconjunto,
    # debemos retroceder para incluirlos.
    # Simplificamos: si el mensaje en start tiene tool_calls, y los siguientes mensajes no cubren todos los tool_calls,
    # retrocedemos.
    # Por simplicidad, solo retrocedemos si el primer mensaje es 'tool' o si el primer mensaje es 'assistant' con tool_calls
    # y el siguiente mensaje no es un 'tool' que corresponda.
    # Implementación más robusta:
    if start > 0 and messages[start]['role'] == 'assistant' and messages[start].get('tool_calls'):
        # Contar cuántos tool_calls hay
        tool_call_ids = [tc['id'] for tc in messages[start]['tool_calls']]
        # Ver si los siguientes mensajes cubren esos ids
        next_msgs = messages[start+1:start+len(tool_call_ids)+1]
        found_ids = [m['tool_call_id'] for m in next_msgs if m['role'] == 'tool']
        if not all(tid in found_ids for tid in tool_call_ids):
            start -= 1  # retrocedemos uno para incluir el mensaje anterior (que podría ser otro assistant con tool_calls o user)
            # Podríamos hacer un bucle, pero con un solo retroceso suele ser suficiente.
    
    # Asegurar que no nos pasemos
    start = max(0, start)
    return messages[start:]

# ==================== MANEJO DE AUDIO ====================
def transcribir_audio_buffer(buffer: io.BytesIO) -> str:
    """Transcribe un buffer de audio usando Groq (síncrono, pero se ejecuta en hilo)."""
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
                language="es"
            )
        return transcription.text
    except Exception as e:
        logger.error(f"Error transcribiendo audio: {e}")
        return ""
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

# ==================== BUCLE PRINCIPAL DEL AGENTE (ASÍNCRONO) ====================
async def procesar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str):
    """Procesa el mensaje del usuario usando el agente con tools (totalmente asíncrono)."""
    if not texto:
        await update.message.reply_text("No entendí el mensaje. ¿Puedes repetirlo?")
        return

    # Inicializar historial si no existe
    if 'messages' not in context.user_data:
        context.user_data['messages'] = []

    # Agregar mensaje del usuario al historial
    context.user_data['messages'].append({"role": "user", "content": texto})

    # Poda del historial (antes de enviar a la API)
    historial_podado = podar_historial(context.user_data['messages'])

    # Inyectar mensaje de sistema con fecha/hora actual (dinámico)
    fecha_actual = ahora_cdmx().strftime('%Y-%m-%d %H:%M')
    system_msg = {
        "role": "system",
        "content": f"La fecha y hora actual en México es: {fecha_actual}. {SYSTEM_PROMPT_BASE}"
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
                max_tokens=800
            )
        except Exception as e:
            logger.error(f"Error llamando a DeepSeek: {e}")
            await update.message.reply_text("❌ Error al comunicarme con el asistente. Intenta de nuevo.")
            return

        message = response.choices[0].message

        # Si no hay tool calls, responder y terminar
        if not message.tool_calls:
            respuesta = message.content
            if respuesta:
                # Guardar en historial (sin el system)
                context.user_data['messages'].append({"role": "assistant", "content": respuesta})
                await update.message.reply_text(respuesta, parse_mode="Markdown")
            else:
                await update.message.reply_text("No tengo respuesta para eso.")
            return

        # Procesar tool calls
        tool_calls = message.tool_calls
        # Agregar el mensaje del asistente con tool_calls al historial
        context.user_data['messages'].append(message.model_dump())

        for tool_call in tool_calls:
            function_name = tool_call.function.name
            function_args = json.loads(tool_call.function.arguments)

            # Ejecutar la función tool en un hilo separado (asyncio.to_thread)
            tool_func = TOOL_FUNCTIONS.get(function_name)
            if not tool_func:
                result = {"error": f"Tool '{function_name}' no encontrada."}
            else:
                try:
                    # Ejecutar en hilo para no bloquear el event loop
                    result = await asyncio.to_thread(tool_func, **function_args)
                except Exception as e:
                    logger.error(f"Error ejecutando tool {function_name}: {e}")
                    result = {"error": str(e)}

            # Agregar el resultado de la tool al historial
            context.user_data['messages'].append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": json.dumps(result)
            })

        # Después de agregar los mensajes tool, reiniciamos el loop para que el LLM procese los resultados
        # Actualizar mensajes_api con el nuevo historial (incluyendo los tools)
        # Volvemos a podar
        historial_podado = podar_historial(context.user_data['messages'])
        mensajes_api = [system_msg] + historial_podado
        # Continuar el bucle

    # Si se excede el número de iteraciones
    await update.message.reply_text("El proceso ha tomado demasiados pasos. Por favor, simplifica tu solicitud.")

# ==================== MANEJADORES DE TELEGRAM ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start: resetea el historial y da la bienvenida."""
    context.user_data['messages'] = []
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
            await update.message.reply_text("❌ El servicio de transcripción de voz no está configurado.")
            return
        try:
            await update.message.reply_text("🎙️ Escuchando...")
            voice_file = await update.message.voice.get_file()
            buffer = io.BytesIO()
            await voice_file.download_to_memory(buffer)
            # Transcribir en hilo separado (bloqueante)
            texto = await asyncio.to_thread(transcribir_audio_buffer, buffer)
            if not texto:
                await update.message.reply_text("❌ No pude entender el audio. ¿Puedes repetirlo o escribirlo?")
                return
            await update.message.reply_text(f"📝 *\"{texto}\"*", parse_mode="Markdown")
            await procesar_mensaje(update, context, texto)
        except Exception as e:
            logger.error(f"Error manejando audio: {e}")
            await update.message.reply_text("❌ Error al procesar el audio. Intenta de nuevo.")

# ==================== INICIO ====================
if __name__ == '__main__':
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handler))
    app.add_handler(MessageHandler(filters.VOICE, handler))
    logger.info("🤖 Bot asíncrono con Tool Calling, poda de historial y contexto temporal iniciado.")
    app.run_polling()
