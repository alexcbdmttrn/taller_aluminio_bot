import os
import json
import re
import logging
import psycopg2
from decimal import Decimal
from datetime import datetime, timedelta
import pytz
from openai import OpenAI
from groq import Groq
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# ==================== CONFIGURACIÓN ====================
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TOKEN = os.environ.get("TELEGRAM_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

deepseek_client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
groq_client = Groq(api_key=GROQ_API_KEY)

# Zona horaria de México (CDMX)
ZONA_HORARIA = pytz.timezone('America/Mexico_City')

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def ahora_cdmx():
    """Devuelve la fecha/hora actual en zona horaria de México (CDMX)."""
    return datetime.now(ZONA_HORARIA)

# ==================== CONSTANTES PARA VALIDACIONES ====================
ACCIONES_VALIDAS = {
    "registrar_proyecto", "registrar_pago", "actualizar_proyecto",
    "marcar_presupuesto_enviado", "cancelar_proyecto", "iniciar_borrado",
    "consultar", "consultar_historial", "consultar_material", "consultar_presupuesto",
    "registrar_compra_material", "registrar_gasto", "consultar_gastos", "preguntar",
    "fusionar_proyectos"
}

ACCIONES_QUE_REQUIEREN_CLIENTE = {
    "registrar_proyecto", "registrar_pago", "actualizar_proyecto",
    "marcar_presupuesto_enviado", "cancelar_proyecto",
    "consultar_material", "consultar_presupuesto", "registrar_compra_material",
    "fusionar_proyectos"
}

_PALABRAS_PAGO = ("anticipo", "anticipó", "abono", "abonó", "ya pagó", "ya pago", "dio un pago")

_PALABRAS_ESCAPE = (
    "olvidalo", "olvídalo", "cancelar eso", "ya no", "déjalo", "dejalo",
    "olvida eso", "cancela eso", "ya no importa", "mejor no", "cancelar todo"
)

_PALABRAS_CONFIRMACION = ("si", "sí", "yes", "sip", "va", "dale", "adelante", "confirmo", "correcto")

_PALABRAS_DESCRIPCION_TRABAJO = ("ventana", "cancel", "puerta", "medida", "cristal", "aluminio", "barandal", "reja", "domo")

ORDEN_ESTADOS = ["Cancelado", "Pendiente de cotizar", "Presupuesto enviado",
                  "Aceptado", "En proceso", "Por cobrar", "Liquidado"]

# ==================== FUNCIONES DE VALIDACIÓN Y CORRECCIÓN ====================
def validar_accion(datos: dict) -> str | None:
    if not isinstance(datos, dict):
        return "la respuesta de la IA no es un objeto JSON válido"
    accion = datos.get("accion")
    if accion not in ACCIONES_VALIDAS:
        return f"acción desconocida: '{accion}'"
    if accion in ACCIONES_QUE_REQUIEREN_CLIENTE:
        cliente = datos.get("cliente")
        if not cliente or cliente == "Desconocido":
            return f"la acción '{accion}' necesita un cliente y no vino ninguno"
    if accion in ("registrar_proyecto", "registrar_pago", "actualizar_proyecto") \
            and not isinstance(datos.get("monto", 0), (int, float)):
        return "el campo 'monto' no es numérico"
    return None

def _corregir_accion_con_texto(datos: dict, texto_original: str) -> dict:
    texto_low = texto_original.lower()
    accion = datos.get("accion")
    if accion == "registrar_proyecto" and any(p in texto_low for p in _PALABRAS_PAGO):
        datos = dict(datos)
        datos["accion"] = "registrar_pago"
    # Si la acción es actualizar y NO hay señales de descripción de trabajo, forzar descripcion vacía
    if accion == "actualizar_proyecto" and not _parece_descripcion_de_trabajo(texto_original):
        datos = dict(datos)
        datos["descripcion"] = ""
    return datos

def _es_escape(texto: str) -> bool:
    t = texto.lower().strip()
    return t in _PALABRAS_ESCAPE or any(t.startswith(p) for p in _PALABRAS_ESCAPE)

def _es_confirmacion(texto: str) -> bool:
    t = texto.strip().lower()
    return any(t == p or t.startswith(p + " ") or t.startswith(p + ",") for p in _PALABRAS_CONFIRMACION)

def _es_respuesta_numerica_simple(texto: str) -> float | None:
    """Devuelve el número si el mensaje ES una respuesta numérica corta
    (ej: '4500', '$4,500', '4500 pesos'), o None si es una frase con otro
    propósito (aunque contenga dígitos, como una corrección o descripción)."""
    t = texto.strip().lower()
    t_limpio = t.replace('$', '').replace(',', '').replace('pesos', '').replace('peso', '').strip()
    palabras = t_limpio.split()
    if len(palabras) == 0 or len(palabras) > 2:
        return None
    try:
        return float(palabras[0])
    except ValueError:
        return None

def _parece_descripcion_de_trabajo(texto_original: str) -> bool:
    t = texto_original.lower()
    return any(p in t for p in _PALABRAS_DESCRIPCION_TRABAJO)

# ==================== ÚLTIMA LISTA (numeración persistente) ====================
def _guardar_ultima_lista(context, items, tipo="proyectos"):
    """items: lista de tuplas (id, cliente, nombre_corto, descripcion, monto, estado)
    en el mismo orden en que se mostraron numeradas al jefe."""
    context.user_data['ultima_lista'] = {
        'tipo': tipo,
        'items': items,
        'timestamp': ahora_cdmx().isoformat()
    }

def _resolver_numero_de_ultima_lista(context, texto: str):
    """Si el texto menciona un número pequeño y hay una lista reciente (< 10 min),
    devuelve el id correspondiente. Si no aplica, devuelve None."""
    ultima = context.user_data.get('ultima_lista')
    if not ultima:
        return None
    try:
        ts = datetime.fromisoformat(ultima['timestamp'])
        if (ahora_cdmx() - ts).total_seconds() > 600:  # 10 minutos de vigencia
            return None
    except Exception:
        return None
    numeros = re.findall(r'\b(\d+)\b', texto)
    if len(numeros) != 1:
        return None
    idx = int(numeros[0]) - 1
    items = ultima['items']
    if 0 <= idx < len(items):
        return items[idx][0]
    return None

_PATRON_EDITAR_POR_NUMERO = re.compile(r'\b(edita|cambia|actualiza|el)\s+(?:el\s+)?(?:proyecto\s+)?(\d+)\b')

# ==================== INTELIGENCIA ARTIFICIAL ====================
def analizar_con_ia(texto, historial_mensajes, cliente_activo="", proyectos_existentes=""):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT nombre FROM clientes ORDER BY nombre")
    clientes_reales = [row[0] for row in cur.fetchall()]
    cur.close()
    conn.close()

    contexto_historial = ""
    if historial_mensajes:
        contexto_historial = "[Historial de los últimos mensajes]:\n"
        for msg in historial_mensajes[-12:]:
            contexto_historial += f"- {msg}\n"
    
    texto_analisis = texto
    if texto.lower() in ['si', 'sí', 'esta bien', 'ok', 'okey', 'correcto', 'bien', 'confirmo']:
        for msg in reversed(historial_mensajes[-5:]):
            if "Nuevo proyecto guardado" in msg or "proyecto" in msg or "¿" in msg:
                texto_analisis = f"CONFIRMACIÓN: El jefe dice '{texto}'. Esto confirma el proyecto/pregunta anterior. NO CREES NUEVO PROYECTO, solo confirma."
                break
    
    contexto_actual = f"\n[Mensaje actual del jefe]: {texto_analisis}"
    if cliente_activo:
        contexto_actual += f"\n[Cliente activo actual]: {cliente_activo}"
    if proyectos_existentes:
        contexto_actual += f"\n\n[PROYECTOS EXISTENTES EN BASE DE DATOS para este cliente]:\n{proyectos_existentes}"

    prompt = f"""Eres el secretario experto de un taller de aluminio. Hablas con tono cercano y respetuoso, usando "jefe" o "patrón".

CLIENTES REGISTRADOS (usa estos nombres EXACTOS cuando reconozcas a un cliente):
{', '.join(clientes_reales) if clientes_reales else 'Aún no hay clientes registrados.'}

REGLAS ESTRICTAS:
1. **REGISTRO DE CLIENTE**: Cuando el jefe diga "registra a [nombre]", extrae toda la información que puedas: dirección, teléfono, trabajo, monto (si lo da). Si falta el monto del presupuesto, pregunta una sola vez. Si el jefe responde "pendiente", "no sé" o similar, guarda con monto 0.
2. **NOMBRE DEL PROYECTO (campo `nombre_corto`)**: Debe ser el NOMBRE DEL TRABAJO específico, NO el nombre del cliente. Ejemplos: "Ventana aluminio blanco", "Cancel baño", "3 ventanas negras".
3. **PRESUPUESTO ENVIADO**: Cuando el jefe diga "ya mandé presupuesto", incluye el monto si lo da. **NO reescribas el campo "descripcion" solo porque mencionó un monto** — la descripción es el TRABAJO (materiales, medidas, tipo de pieza). Deja "descripcion" vacía en el JSON a menos que el jefe esté describiendo el trabajo en ese mismo mensaje.
4. **ACTUALIZACIONES**: Si el jefe actualiza el monto, la descripción o cualquier dato, usa "actualizar_proyecto".
5. **CONSULTAS**: "qué clientes tengo" → "consultar" tipo "activos". "liquidados" → "liquidados". "cancelados" → "cancelados".
6. **BORRAR**: "borra", "elimina", "quiero eliminar clientes" → "iniciar_borrado".
7. **GASTOS**: "gasté" sin cliente → "registrar_gasto". "gastos" → "consultar_gastos".
8. **CONFIRMACIONES**: "si", "sí", "esta bien" → NO crees nuevo proyecto, solo confirma lo anterior.
9. **FUSIONAR**: "fusiona", "combina", "une" + proyectos del mismo cliente → "fusionar_proyectos".

Responde SOLO con este JSON (nota que "acciones" es una LISTA):
{{
  "acciones": [
    {{
      "accion": "registrar_proyecto" | "registrar_pago" | "actualizar_proyecto" | "marcar_presupuesto_enviado" | "cancelar_proyecto" | "iniciar_borrado" | "consultar" | "consultar_historial" | "consultar_material" | "consultar_presupuesto" | "registrar_compra_material" | "registrar_gasto" | "consultar_gastos" | "fusionar_proyectos" | "preguntar",
      "cliente": "nombre",
      "nombre_corto": "nombre breve del TRABAJO (ej: 'ventana aluminio', 'cancel baño')",
      "monto": numero o 0,
      "descripcion": "detalles completos del trabajo (solo si se describen en este mensaje)",
      "notas": "",
      "telefono": "",
      "direccion": "",
      "estado": "Pendiente de cotizar" | "Presupuesto enviado" | "Aceptado" | "En proceso" | "Por cobrar" | "Liquidado" | "Cancelado",
      "tipo_borrado": "activos" | "cancelados" | "liquidados" | "gastos",
      "tipo_consulta": "activos" | "cancelados" | "liquidados" | "deudores" | "pendientes" | "todos",
      "pregunta": "texto de la pregunta"
    }}
  ],
  "resumen": "frase corta de TODO lo que harás en este mensaje"
}}

{contexto_historial}
{contexto_actual}

Responde ÚNICAMENTE con el JSON."""

    response = deepseek_client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1
    )
    
    texto_respuesta = response.choices[0].message.content
    texto_limpio = texto_respuesta.replace("```json", "").replace("```", "").strip()
    try:
        resultado = json.loads(texto_limpio)
    except json.JSONDecodeError:
        return {"acciones": [{"accion": "preguntar", "pregunta": "No entendí bien, ¿puedes repetirlo, jefe?"}]}

    acciones_a_validar = resultado.get("acciones") or [resultado]
    errores = [validar_accion(a) for a in acciones_a_validar]
    errores = [e for e in errores if e]
    if errores:
        logging.error(f"🔴 JSON de la IA no pasó validación: {errores}")
        prompt_retry = prompt + f"\n\nTU RESPUESTA ANTERIOR TENÍA ESTE PROBLEMA: {errores}. Corrígelo y responde de nuevo SOLO con el JSON."
        try:
            response2 = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": prompt_retry}],
                temperature=0.1
            )
            texto2 = response2.choices[0].message.content.replace("```json", "").replace("```", "").strip()
            resultado2 = json.loads(texto2)
            acciones2 = resultado2.get("acciones") or [resultado2]
            if not any(validar_accion(a) for a in acciones2):
                return resultado2
        except (json.JSONDecodeError, Exception) as e:
            logging.error(f"🔴 Reintento falló: {e}")
        return {"acciones": [{"accion": "preguntar", "pregunta": "No logré entender bien esa instrucción, ¿me la puedes explicar de otra forma, jefe?"}]}

    return resultado

# ==================== AUDIO (GROQ) ====================
def transcribir_audio(ruta_archivo):
    with open(ruta_archivo, "rb") as file:
        transcription = groq_client.audio.transcriptions.create(
            file=(ruta_archivo, file.read()),
            model="whisper-large-v3",
            language="es"
        )
    return transcription.text

# ==================== BASE DE DATOS ====================
def buscar_o_crear_cliente(cur, nombre_cliente, telefono="", direccion="", notas=""):
    nombre_cliente = nombre_cliente.lower().strip()
    cur.execute("SELECT id FROM clientes WHERE unaccent(nombre) ILIKE unaccent(%s)", (f"%{nombre_cliente}%",))
    cliente = cur.fetchone()
    if cliente:
        cliente_id = cliente[0]
        if telefono or direccion or notas:
            cur.execute("""UPDATE clientes SET 
                           telefono = COALESCE(%s, telefono), 
                           direccion = COALESCE(%s, direccion),
                           notas_adicionales = COALESCE(%s, notas_adicionales)
                           WHERE id = %s""", (telefono or None, direccion or None, notas or None, cliente_id))
        return cliente_id
    else:
        cur.execute("INSERT INTO clientes (nombre, telefono, direccion, notas_adicionales) VALUES (%s, %s, %s, %s) RETURNING id", 
                    (nombre_cliente, telefono or None, direccion or None, notas or None))
        return cur.fetchone()[0]

def obtener_proyectos_activos_por_cliente(cur, cliente_nombre):
    cliente_nombre = cliente_nombre.lower().strip()
    cur.execute("""
        SELECT p.id, p.nombre_corto, p.descripcion, p.monto_total, p.monto_pagado, p.estado,
               p.material_comprado, p.fecha_compra_material, p.costo_material,
               p.presupuesto_enviado, p.fecha_presupuesto, c.nombre as cliente_nombre,
               c.telefono, c.direccion, c.notas_adicionales
        FROM proyectos p 
        JOIN clientes c ON p.cliente_id = c.id 
        WHERE unaccent(c.nombre) ILIKE unaccent(%s) AND p.estado NOT IN ('Liquidado', 'Cancelado')
        ORDER BY p.fecha_creacion DESC
    """, (f"%{cliente_nombre}%",))
    resultados = cur.fetchall()
    if resultados:
        return resultados
    palabras = cliente_nombre.split()
    if len(palabras) > 1:
        condiciones = " AND ".join(["unaccent(c.nombre) ILIKE unaccent(%s)" for _ in palabras])
        params = [f"%{p}%" for p in palabras]
        cur.execute(f"""
            SELECT p.id, p.nombre_corto, p.descripcion, p.monto_total, p.monto_pagado, p.estado,
                   p.material_comprado, p.fecha_compra_material, p.costo_material,
                   p.presupuesto_enviado, p.fecha_presupuesto, c.nombre as cliente_nombre,
                   c.telefono, c.direccion, c.notas_adicionales
            FROM proyectos p 
            JOIN clientes c ON p.cliente_id = c.id 
            WHERE {condiciones} AND p.estado NOT IN ('Liquidado', 'Cancelado')
            ORDER BY p.fecha_creacion DESC
        """, params)
        return cur.fetchall()
    return []

def obtener_proyectos_por_estado(cur, estado):
    estado_lower = estado.lower()
    if estado_lower == "activos":
        condicion = "LOWER(p.estado) NOT IN ('liquidado', 'cancelado')"
    elif estado_lower == "cancelados":
        condicion = "LOWER(p.estado) = 'cancelado'"
    elif estado_lower == "liquidados":
        condicion = "LOWER(p.estado) = 'liquidado'"
    else:
        return []
    cur.execute(f"""
        SELECT c.nombre, p.id, p.nombre_corto, p.descripcion, p.monto_total, p.estado,
               c.telefono, c.direccion, c.notas_adicionales
        FROM proyectos p
        JOIN clientes c ON p.cliente_id = c.id
        WHERE {condicion}
        ORDER BY c.nombre, p.fecha_creacion DESC
    """)
    return cur.fetchall()

def obtener_gastos(cur):
    cur.execute("SELECT id, descripcion, monto, fecha FROM gastos ORDER BY fecha DESC")
    return cur.fetchall()

def registrar_gasto(cur, descripcion, monto):
    cur.execute("INSERT INTO gastos (descripcion, monto) VALUES (%s, %s) RETURNING id", (descripcion, monto))
    return cur.fetchone()[0]

def limpiar_clientes_huérfanos(cur):
    cur.execute("""
        DELETE FROM clientes 
        WHERE id NOT IN (SELECT DISTINCT cliente_id FROM proyectos WHERE cliente_id IS NOT NULL)
    """)
    return cur.rowcount

def borrar_proyectos_por_ids(cur, ids):
    ids_int = []
    for id_item in ids:
        try:
            ids_int.append(int(id_item))
        except (ValueError, TypeError):
            continue
    if not ids_int:
        return 0
    cur.execute("DELETE FROM proyectos WHERE id = ANY(%s)", (ids_int,))
    return cur.rowcount

def borrar_gastos_por_ids(cur, ids):
    ids_int = []
    for id_item in ids:
        try:
            ids_int.append(int(id_item))
        except (ValueError, TypeError):
            continue
    if not ids_int:
        return 0
    cur.execute("DELETE FROM gastos WHERE id = ANY(%s)", (ids_int,))
    return cur.rowcount

def fusionar_proyectos(cur, ids: list[int]):
    """Combina 2+ proyectos en uno solo."""
    if len(ids) < 2:
        return None, "Necesito al menos 2 proyectos para fusionar."
    cur.execute("""
        SELECT id, nombre_corto, descripcion, monto_total, monto_pagado, estado,
               material_comprado, costo_material, presupuesto_enviado, fecha_presupuesto,
               fecha_creacion, cliente_id
        FROM proyectos WHERE id = ANY(%s) ORDER BY fecha_creacion ASC
    """, (ids,))
    filas = cur.fetchall()
    if len(filas) != len(ids):
        return None, "Alguno de esos proyectos ya no existe."
    if len(set(f[11] for f in filas)) > 1:
        return None, "Esos proyectos son de clientes distintos, no se pueden fusionar."
    base = filas[0]
    base_id = base[0]
    nombre_corto = base[1] or next((f[1] for f in filas if f[1]), 'Proyecto General')
    descripcion = " | ".join(dict.fromkeys(f[2] for f in filas if f[2]))
    monto_total = sum(Decimal(str(f[3])) for f in filas)
    monto_pagado = sum(Decimal(str(f[4])) for f in filas)
    estado_final = max((f[5] for f in filas), key=lambda e: ORDEN_ESTADOS.index(e) if e in ORDEN_ESTADOS else 0)
    material_comprado = any(f[6] for f in filas)
    costo_material = sum(Decimal(str(f[7])) for f in filas if f[7]) or None
    presupuesto_enviado = any(f[8] for f in filas)
    fecha_presupuesto = max((f[9] for f in filas if f[9]), default=None)
    cur.execute("""
        UPDATE proyectos SET
            nombre_corto = %s, descripcion = %s, monto_total = %s, monto_pagado = %s,
            estado = %s, material_comprado = %s, costo_material = %s,
            presupuesto_enviado = %s, fecha_presupuesto = %s
        WHERE id = %s
    """, (nombre_corto, descripcion, float(monto_total), float(monto_pagado), estado_final,
          material_comprado, float(costo_material) if costo_material else None,
          presupuesto_enviado, fecha_presupuesto, base_id))
    ids_a_borrar = [f[0] for f in filas if f[0] != base_id]
    if ids_a_borrar:
        cur.execute("DELETE FROM proyectos WHERE id = ANY(%s)", (ids_a_borrar,))
    return base_id, f"Fusionados {len(filas)} proyectos en uno: '{nombre_corto}' — Total: ${float(monto_total):.2f}, Pagado: ${float(monto_pagado):.2f}, Estado: {estado_final}"

# ==================== FUNCIONES DE PROYECTOS ====================
def obtener_proyectos_activos(cur, cliente_nombre):
    cliente_nombre = cliente_nombre.lower().strip()
    cur.execute("""
        SELECT p.id, p.nombre_corto, p.descripcion, p.monto_total, p.monto_pagado, p.estado,
               p.material_comprado, p.fecha_compra_material, p.costo_material,
               p.presupuesto_enviado, p.fecha_presupuesto,
               c.telefono, c.direccion, c.notas_adicionales
        FROM proyectos p 
        JOIN clientes c ON p.cliente_id = c.id 
        WHERE unaccent(c.nombre) ILIKE unaccent(%s) AND p.estado NOT IN ('Liquidado', 'Cancelado')
        ORDER BY p.fecha_creacion DESC
    """, (f"%{cliente_nombre}%",))
    return cur.fetchall()

def obtener_historial_cliente(cur, cliente_nombre):
    cliente_nombre = cliente_nombre.lower().strip()
    cur.execute("""
        SELECT p.nombre_corto, p.descripcion, p.monto_total, p.monto_pagado, p.estado, p.fecha_creacion,
               p.material_comprado, p.fecha_compra_material, p.costo_material,
               p.presupuesto_enviado, p.fecha_presupuesto
        FROM proyectos p 
        JOIN clientes c ON p.cliente_id = c.id 
        WHERE unaccent(c.nombre) ILIKE unaccent(%s)
        ORDER BY p.fecha_creacion DESC
    """, (f"%{cliente_nombre}%",))
    return cur.fetchall()

def obtener_estadisticas(cur):
    cur.execute("SELECT estado, COUNT(*), SUM(monto_total) FROM proyectos GROUP BY estado")
    return cur.fetchall()

def registrar_proyecto(cur, cliente_id, nombre_corto, descripcion, monto, estado, notas=""):
    cur.execute("""INSERT INTO proyectos (cliente_id, nombre_corto, descripcion, monto_total, monto_pagado, estado, notas_adicionales) 
                   VALUES (%s, %s, %s, %s, 0, %s, %s) RETURNING id""", 
                (cliente_id, nombre_corto or 'Proyecto General', descripcion, monto, estado, notas or None))
    return cur.fetchone()[0]

def resolver_proyecto_activo(cur, cliente_nombre, referencia):
    cliente_nombre = cliente_nombre.lower().strip()
    referencia = (referencia or "").strip()
    if referencia and referencia.lower() not in ("proyecto general", "desconocido", ""):
        cur.execute("""
            SELECT p.id, p.nombre_corto FROM proyectos p
            JOIN clientes c ON p.cliente_id = c.id
            WHERE unaccent(c.nombre) ILIKE unaccent(%s) AND p.estado NOT IN ('Liquidado', 'Cancelado')
            AND (unaccent(p.nombre_corto) ILIKE unaccent(%s) OR unaccent(p.descripcion) ILIKE unaccent(%s))
            ORDER BY p.fecha_creacion DESC
        """, (f"%{cliente_nombre}%", f"%{referencia}%", f"%{referencia}%"))
        coincidencias = cur.fetchall()
        if len(coincidencias) == 1:
            return coincidencias[0][0], []
        if len(coincidencias) > 1:
            return None, coincidencias
    cur.execute("""
        SELECT p.id, p.nombre_corto FROM proyectos p
        JOIN clientes c ON p.cliente_id = c.id
        WHERE unaccent(c.nombre) ILIKE unaccent(%s) AND p.estado NOT IN ('Liquidado', 'Cancelado')
        ORDER BY p.fecha_creacion DESC
    """, (f"%{cliente_nombre}%",))
    activos = cur.fetchall()
    if len(activos) == 1:
        return activos[0][0], []
    return None, activos

def actualizar_proyecto(cur, cliente_nombre, nombre_corto, descripcion, monto, estado, notas="", presupuesto_enviado=None, proyecto_id_forced=None):
    cliente_nombre = cliente_nombre.lower().strip()
    if proyecto_id_forced:
        proyecto_id = proyecto_id_forced
        candidatos = []
    else:
        proyecto_id, candidatos = resolver_proyecto_activo(cur, cliente_nombre, nombre_corto or descripcion)
    if proyecto_id is None:
        return False, candidatos
    updates = ["nombre_corto = COALESCE(%s, nombre_corto)",
               "monto_total = CASE WHEN %s > 0 THEN %s ELSE monto_total END",
               "estado = COALESCE(%s, estado)", "notas_adicionales = COALESCE(%s, notas_adicionales)"]
    params = [nombre_corto or None, monto, monto, estado or None, notas or None]
    # Solo actualizar descripcion si vino con contenido (no vacío)
    if descripcion and descripcion.strip():
        updates.append("descripcion = COALESCE(%s, descripcion)")
        params.append(descripcion)
    if presupuesto_enviado is not None:
        updates.append("presupuesto_enviado = %s")
        params.append(presupuesto_enviado)
        if presupuesto_enviado:
            updates.append("fecha_presupuesto = CURRENT_TIMESTAMP")
    params.append(proyecto_id)
    cur.execute(f"""
        UPDATE proyectos 
        SET {", ".join(updates)}
        WHERE id = %s
    """, tuple(params))
    return True, []

def marcar_presupuesto_enviado(cur, cliente_nombre, nombre_corto, monto=None, descripcion=None, telefono=None, direccion=None, notas=None):
    cliente_nombre = cliente_nombre.lower().strip()
    if telefono or direccion or notas:
        buscar_o_crear_cliente(cur, cliente_nombre, telefono, direccion, notas)
    proyecto_id, candidatos = resolver_proyecto_activo(cur, cliente_nombre, nombre_corto or descripcion)
    if proyecto_id is None:
        return False, candidatos
    updates = ["presupuesto_enviado = TRUE", "fecha_presupuesto = CURRENT_TIMESTAMP", "estado = 'Presupuesto enviado'"]
    params = []
    if monto is not None and monto > 0:
        updates.append("monto_total = %s")
        params.append(monto)
    if descripcion and descripcion.strip():
        updates.append("descripcion = COALESCE(%s, descripcion)")
        params.append(descripcion)
    if notas:
        updates.append("notas_adicionales = COALESCE(%s, notas_adicionales)")
        params.append(notas)
    params.append(proyecto_id)
    cur.execute(f"""
        UPDATE proyectos 
        SET {', '.join(updates)}
        WHERE id = %s
    """, tuple(params))
    return True, []

def cancelar_proyecto_especifico(cur, cliente_nombre, nombre_corto):
    cliente_nombre = cliente_nombre.lower().strip()
    cur.execute("""
        SELECT p.id FROM proyectos p
        JOIN clientes c ON p.cliente_id = c.id
        WHERE unaccent(c.nombre) ILIKE unaccent(%s) AND (unaccent(p.nombre_corto) ILIKE unaccent(%s) OR unaccent(p.descripcion) ILIKE unaccent(%s))
        AND p.estado NOT IN ('Liquidado', 'Cancelado')
        ORDER BY p.fecha_creacion DESC LIMIT 1
    """, (f"%{cliente_nombre}%", f"%{nombre_corto}%", f"%{nombre_corto}%"))
    proyecto = cur.fetchone()
    if proyecto:
        cur.execute("UPDATE proyectos SET estado = 'Cancelado' WHERE id = %s", (proyecto[0],))
        return True
    return False

def registrar_pago(cur, cliente_nombre, monto_pago, referencia=""):
    cliente_nombre = cliente_nombre.lower().strip()
    monto_pago = Decimal(str(monto_pago))
    proyecto_id, candidatos = resolver_proyecto_activo(cur, cliente_nombre, referencia)
    if proyecto_id is None:
        return None, None, candidatos
    cur.execute("SELECT monto_total, monto_pagado, estado, nombre_corto FROM proyectos WHERE id = %s", (proyecto_id,))
    row = cur.fetchone()
    if not row:
        return None, None, []
    monto_total, monto_pagado_actual, estado_actual, nombre_corto = row
    monto_total = Decimal(str(monto_total))
    monto_pagado_actual = Decimal(str(monto_pagado_actual))
    nuevo_pagado = monto_pagado_actual + monto_pago
    saldo = max(Decimal('0'), monto_total - nuevo_pagado)
    nuevo_estado = "Liquidado" if saldo == 0 else "Por cobrar"
    if monto_pago > 0 and estado_actual == "Pendiente de cotizar":
        nuevo_estado = "En proceso"
    cur.execute("UPDATE proyectos SET monto_pagado = %s, estado = %s WHERE id = %s",
                (float(nuevo_pagado), nuevo_estado, proyecto_id))
    msg = f"{nombre_corto}: Anticipo/Pago ${float(monto_pago):.2f}. Saldo: ${float(saldo):.2f}. Estado: {nuevo_estado}"
    return proyecto_id, msg, []

def marcar_material_comprado(cur, proyecto_id, costo=None):
    if costo is not None:
        costo = Decimal(str(costo))
    cur.execute("""
        UPDATE proyectos 
        SET material_comprado = TRUE, 
            fecha_compra_material = CURRENT_TIMESTAMP,
            costo_material = COALESCE(%s, costo_material)
        WHERE id = %s
    """, (float(costo) if costo is not None else None, proyecto_id))
    return cur.rowcount > 0

def consultar_material_cliente(cur, cliente_nombre):
    cliente_nombre = cliente_nombre.lower().strip()
    cur.execute("""
        SELECT p.nombre_corto, p.descripcion, p.material_comprado, p.fecha_compra_material, p.costo_material
        FROM proyectos p 
        JOIN clientes c ON p.cliente_id = c.id 
        WHERE unaccent(c.nombre) ILIKE unaccent(%s) AND p.estado NOT IN ('Liquidado', 'Cancelado')
        ORDER BY p.fecha_creacion DESC
    """, (f"%{cliente_nombre}%",))
    return cur.fetchall()

def consultar_presupuesto_cliente(cur, cliente_nombre):
    cliente_nombre = cliente_nombre.lower().strip()
    cur.execute("""
        SELECT p.nombre_corto, p.descripcion, p.presupuesto_enviado, p.fecha_presupuesto, p.monto_total
        FROM proyectos p 
        JOIN clientes c ON p.cliente_id = c.id 
        WHERE unaccent(c.nombre) ILIKE unaccent(%s) AND p.estado NOT IN ('Liquidado', 'Cancelado')
        ORDER BY p.fecha_creacion DESC
    """, (f"%{cliente_nombre}%",))
    return cur.fetchall()

def consultar_proyectos(cur, tipo_consulta, cliente_nombre=None):
    tipo_lower = tipo_consulta.lower()
    if cliente_nombre:
        cliente_nombre = cliente_nombre.lower().strip()
    if tipo_lower == "cancelados":
        if cliente_nombre:
            cur.execute("""
                SELECT c.nombre, p.nombre_corto, p.descripcion, p.monto_total, p.monto_pagado, p.estado,
                       p.presupuesto_enviado, p.material_comprado,
                       c.telefono, c.direccion, c.notas_adicionales
                FROM proyectos p JOIN clientes c ON p.cliente_id = c.id 
                WHERE unaccent(c.nombre) ILIKE unaccent(%s) AND LOWER(p.estado) = 'cancelado'
                ORDER BY p.fecha_creacion DESC
            """, (f"%{cliente_nombre}%",))
        else:
            cur.execute("""
                SELECT c.nombre, p.nombre_corto, p.descripcion, p.monto_total, p.monto_pagado, p.estado,
                       p.presupuesto_enviado, p.material_comprado,
                       c.telefono, c.direccion, c.notas_adicionales
                FROM proyectos p JOIN clientes c ON p.cliente_id = c.id 
                WHERE LOWER(p.estado) = 'cancelado'
                ORDER BY c.nombre, p.fecha_creacion DESC
            """)
        return cur.fetchall()
    elif tipo_lower == "liquidados":
        if cliente_nombre:
            cur.execute("""
                SELECT c.nombre, p.nombre_corto, p.descripcion, p.monto_total, p.monto_pagado, p.estado,
                       p.presupuesto_enviado, p.material_comprado,
                       c.telefono, c.direccion, c.notas_adicionales
                FROM proyectos p JOIN clientes c ON p.cliente_id = c.id 
                WHERE unaccent(c.nombre) ILIKE unaccent(%s) AND LOWER(p.estado) = 'liquidado'
                ORDER BY p.fecha_creacion DESC
            """, (f"%{cliente_nombre}%",))
        else:
            cur.execute("""
                SELECT c.nombre, p.nombre_corto, p.descripcion, p.monto_total, p.monto_pagado, p.estado,
                       p.presupuesto_enviado, p.material_comprado,
                       c.telefono, c.direccion, c.notas_adicionales
                FROM proyectos p JOIN clientes c ON p.cliente_id = c.id 
                WHERE LOWER(p.estado) = 'liquidado'
                ORDER BY c.nombre, p.fecha_creacion DESC
            """)
        return cur.fetchall()
    else:  # activos
        if cliente_nombre:
            cur.execute("""
                SELECT c.nombre, p.nombre_corto, p.descripcion, p.monto_total, p.monto_pagado, p.estado,
                       p.presupuesto_enviado, p.material_comprado,
                       c.telefono, c.direccion, c.notas_adicionales
                FROM proyectos p JOIN clientes c ON p.cliente_id = c.id 
                WHERE unaccent(c.nombre) ILIKE unaccent(%s) AND LOWER(p.estado) NOT IN ('liquidado', 'cancelado')
                ORDER BY p.fecha_creacion DESC
            """, (f"%{cliente_nombre}%",))
        else:
            cur.execute("""
                SELECT c.nombre, p.nombre_corto, p.descripcion, p.monto_total, p.monto_pagado, p.estado,
                       p.presupuesto_enviado, p.material_comprado,
                       c.telefono, c.direccion, c.notas_adicionales
                FROM proyectos p JOIN clientes c ON p.cliente_id = c.id 
                WHERE LOWER(p.estado) NOT IN ('liquidado', 'cancelado')
                ORDER BY c.nombre, p.fecha_creacion DESC
            """)
        return cur.fetchall()

# ==================== COMANDOS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['historial'] = []
    context.user_data['cliente_activo'] = ""
    await update.message.reply_text(
        "¡Hola, jefe! 🛠️ Su secretario está listo.\n\n"
        "Hable conmigo como lo haría con su asistente.\n"
        "Ejemplos:\n"
        "- 'Registra a Juan Pérez, calle siempre viva 123, tel 5551234, 2 ventanas negras'\n"
        "- 'El presupuesto es de 8000'\n"
        "- 'Ya mandé presupuesto a Juan por 8000'\n"
        "- 'Quiero eliminar clientes'\n"
        "- 'Fusiona los proyectos 1 y 2 de Juan'\n"
        "Si no tiene el presupuesto, diga 'pendiente' y lo guardo igual."
    )

async def comando_cliente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        cliente_nombre = " ".join(context.args)
        context.user_data['cliente_activo'] = cliente_nombre.lower()
        await update.message.reply_text(f"✅ Cliente activo: **{cliente_nombre}**", parse_mode='Markdown')
    else:
        actual = context.user_data.get('cliente_activo', 'Ninguno')
        await update.message.reply_text(f"📌 Cliente activo: **{actual}**", parse_mode='Markdown')

async def comando_estadisticas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        stats = obtener_estadisticas(cur)
        cur.close(); conn.close()
        if not stats:
            await update.message.reply_text("📊 Aún no hay datos, jefe.")
            return
        msg = "📊 **ESTADÍSTICAS:**\n\n"
        total = 0
        for estado, cantidad, monto_total in stats:
            monto_total = monto_total or 0
            msg += f"🔹 *{estado}:* {cantidad} proyectos (${monto_total:.2f})\n"
            total += cantidad
        msg += f"\n📌 Total: {total}"
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def comando_historial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usa: /historial Nombre, jefe.")
        return
    nombre = " ".join(context.args).lower().strip()
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT nombre, telefono, direccion FROM clientes WHERE unaccent(nombre) ILIKE unaccent(%s)", (f"%{nombre}%",))
        cliente = cur.fetchone()
        if not cliente:
            await update.message.reply_text(f"⚠️ No tengo registros de '{nombre}', jefe.")
            cur.close(); conn.close(); return
        nombre_real, tel, dir = cliente
        historial = obtener_historial_cliente(cur, nombre_real)
        cur.close(); conn.close()
        msg = f"📂 **HISTORIAL DE {nombre_real.upper()}**\n"
        if tel: msg += f"📞 Tel: {tel}\n"
        if dir: msg += f"📍 Dir: {dir}\n"
        msg += "─────────────────\n"
        if not historial:
            msg += "Sin proyectos."
        else:
            for nc, desc, total, pagado, estado, fecha, mat_comp, fecha_mat, costo_mat, pres_comp, fecha_pres in historial:
                saldo = total - (pagado or 0)
                icono = "✅" if estado == "Liquidado" else "🗑️" if estado == "Cancelado" else "⏳"
                msg += f"{icono} *{nc or 'Proyecto'}* ({estado})\n   {desc}\n"
                msg += f"   Total: ${total} | Pagado: ${pagado or 0} | Saldo: ${saldo}\n"
                if pres_comp:
                    fecha_str = fecha_pres.strftime("%d/%m %H:%M") if fecha_pres else "Fecha desconocida"
                    msg += f"   📋 Presupuesto enviado: {fecha_str}\n"
                if mat_comp:
                    fecha_str = fecha_mat.strftime("%d/%m %H:%M") if fecha_mat else "Fecha desconocida"
                    costo = costo_mat if costo_mat else "sin costo registrado"
                    msg += f"   🛠️ Material comprado: {fecha_str} (${costo})\n"
                msg += "\n"
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def comando_resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        proyectos = consultar_proyectos(cur, "activos")
        cur.close(); conn.close()
        if not proyectos:
            await update.message.reply_text("📭 No hay clientes activos, jefe.")
            return
        msg = "📊 **CLIENTES ACTIVOS:**\n\n"
        for n, nc, desc, t, p, e, pres_comp, mat_comp, tel, dir, notas in proyectos:
            pendiente = t - p
            pres_icon = "📋" if pres_comp else "⏳"
            mat_icon = "🛠️" if mat_comp else "❌"
            msg += f"👤 *{n}*\n"
            msg += f"   🔧 Proyecto: {nc or 'Proyecto General'}\n"
            msg += f"   📝 Detalle: {desc}\n"
            if tel:
                msg += f"   📞 Tel: {tel}\n"
            if dir:
                msg += f"   📍 Dirección: {dir}\n"
            if notas:
                msg += f"   📌 Notas: {notas}\n"
            msg += f"   💰 Total: ${t:.2f} | Pagado: ${p:.2f} | Saldo: ${pendiente:.2f}\n"
            msg += f"   📌 Estado: {e} | {pres_icon} Presupuesto | {mat_icon} Material\n\n"
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def comando_material(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usa: /material Nombre, jefe. Ej: /material Pedro")
        return
    nombre = " ".join(context.args).lower().strip()
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        registros = consultar_material_cliente(cur, nombre)
        cur.close(); conn.close()
        if not registros:
            await update.message.reply_text(f"🔍 No tengo proyectos activos para {nombre}, jefe.")
            return
        msg = f"🛠️ **Estado de material para {nombre}:**\n\n"
        for nc, desc, comprado, fecha, costo in registros:
            if comprado:
                fecha_str = fecha.strftime("%d/%m %H:%M") if fecha else "fecha desconocida"
                costo_str = f" (${costo})" if costo else ""
                msg += f"✅ *{nc or 'Proyecto'}*: Comprado el {fecha_str}{costo_str}\n   {desc}\n\n"
            else:
                msg += f"❌ *{nc or 'Proyecto'}*: **No comprado aún**\n   {desc}\n\n"
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def comando_presupuesto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usa: /presupuesto Nombre, jefe. Ej: /presupuesto Pedro")
        return
    nombre = " ".join(context.args).lower().strip()
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        registros = consultar_presupuesto_cliente(cur, nombre)
        cur.close(); conn.close()
        if not registros:
            await update.message.reply_text(f"🔍 No tengo proyectos activos para {nombre}, jefe.")
            return
        msg = f"📋 **Estado de presupuestos para {nombre}:**\n\n"
        for nc, desc, enviado, fecha, monto in registros:
            if enviado:
                fecha_str = fecha.strftime("%d/%m %H:%M") if fecha else "fecha desconocida"
                msg += f"✅ *{nc or 'Proyecto'}*: Enviado el {fecha_str} (${monto:.2f})\n   {desc}\n\n"
            else:
                msg += f"❌ *{nc or 'Proyecto'}*: **No enviado aún**\n   {desc}\n\n"
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def comando_gastos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        gastos = obtener_gastos(cur)
        cur.close(); conn.close()
        if not gastos:
            await update.message.reply_text("📭 No hay gastos registrados aún, jefe.")
            return
        msg = "🧾 **ÚLTIMOS GASTOS:**\n\n"
        for gid, desc, monto, fecha in gastos[:10]:
            fecha_str = fecha.strftime("%d/%m %H:%M") if fecha else "Fecha desconocida"
            msg += f"💸 {fecha_str} | ${monto:.2f} - {desc}\n"
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

# ==================== MANEJO DE BORRADO ====================
async def iniciar_borrado(update: Update, context: ContextTypes.DEFAULT_TYPE, tipo=None, cliente=None):
    if tipo is None:
        await update.message.reply_text(
            "🤔 ¿Qué tipo de elementos quieres borrar?\n\n"
            "1. Activos (proyectos en curso)\n"
            "2. Cancelados\n"
            "3. Liquidados\n"
            "4. Gastos\n\n"
            "Responde con el número (1,2,3,4)."
        )
        context.user_data['estado_espera'] = 'esperando_tipo_borrado'
        return

    conn = get_db_connection()
    cur = conn.cursor()

    if tipo == "gastos":
        items = obtener_gastos(cur)
        cur.close(); conn.close()
        if not items:
            await update.message.reply_text(f"📭 No hay gastos para borrar, jefe.")
            return
        msg = "💸 **GASTOS** (elige números separados por comas, o 'todos'):\n\n"
        for idx, (gid, desc, monto, fecha) in enumerate(items, 1):
            fecha_str = fecha.strftime("%d/%m %H:%M") if fecha else "Fecha desconocida"
            msg += f"{idx}. 💸 {fecha_str} | ${monto:.2f} - {desc}\n"
        context.user_data['borrar_tipo'] = 'gastos'
        context.user_data['borrar_items'] = items
    else:
        if cliente:
            if tipo == "activos":
                items_raw = obtener_proyectos_activos_por_cliente(cur, cliente)
                items = []
                for row in items_raw:
                    cliente_nombre_db = row[11] if len(row) > 11 else "Desconocido"
                    items.append((row[0], cliente_nombre_db, row[1], row[2], row[3], row[5]))
                label = "activos"
                icono = "📋"
            else:
                items_raw = obtener_proyectos_por_estado(cur, tipo)
                palabras = cliente.split()
                items = []
                for item in items_raw:
                    nombre_cliente_db = item[0]
                    for p in palabras:
                        cur_temp = conn.cursor()
                        cur_temp.execute("SELECT unaccent(%s) ILIKE unaccent(%s)", (nombre_cliente_db, f"%{p}%"))
                        coincide = cur_temp.fetchone()[0]
                        cur_temp.close()
                        if coincide:
                            items.append((item[1], item[0], item[2], item[3], item[4], item[5]))
                            break
                label = tipo
                icono = "🗑️" if tipo == "cancelados" else "✅" if tipo == "liquidados" else "📋"
        else:
            items_raw = obtener_proyectos_por_estado(cur, tipo)
            items = [(item[1], item[0], item[2], item[3], item[4], item[5]) for item in items_raw]
            label = tipo
            icono = "📋" if tipo == "activos" else "🗑️" if tipo == "cancelados" else "✅"

        cur.close(); conn.close()
        if not items:
            conn_debug = get_db_connection()
            cur_debug = conn_debug.cursor()
            cur_debug.execute("SELECT DISTINCT estado FROM proyectos")
            estados_reales = [row[0] for row in cur_debug.fetchall()]
            cur_debug.close(); conn_debug.close()
            if cliente:
                await update.message.reply_text(
                    f"📭 No encontré proyectos {label} para {cliente}, jefe.\n\n"
                    f"Estados reales en la BD: {', '.join(estados_reales) if estados_reales else 'ninguno'}"
                )
            else:
                await update.message.reply_text(
                    f"📭 No hay proyectos {label} para borrar, jefe.\n\n"
                    f"Estados reales en la BD: {', '.join(estados_reales) if estados_reales else 'ninguno'}"
                )
            return

        if cliente:
            msg = f"{icono} **PROYECTOS {label.upper()} DE {cliente.upper()}** (elige números separados por comas, o 'todos'):\n\n"
        else:
            msg = f"{icono} **PROYECTOS {label.upper()}** (elige números separados por comas, o 'todos'):\n\n"

        for idx, (pid, cliente_db, nc, desc, monto, estado) in enumerate(items, 1):
            msg += f"{idx}. 👤 {cliente_db} | {nc or 'Proyecto General'} - ${monto:.2f} ({estado})\n   {desc}\n\n"

        context.user_data['borrar_tipo'] = 'proyectos'
        context.user_data['borrar_items'] = items
        _guardar_ultima_lista(context, items, tipo="borrar")

    context.user_data['estado_espera'] = 'esperando_seleccion_borrado'
    await update.message.reply_text(msg, parse_mode='Markdown')

async def procesar_seleccion_borrado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    respuesta = update.message.text
    try:
        items = context.user_data.get('borrar_items', [])
        tipo = context.user_data.get('borrar_tipo')
        if not items or not tipo:
            await update.message.reply_text("⚠️ No tengo elementos en memoria, jefe.")
            context.user_data['estado_espera'] = None
            return

        resp = respuesta.strip().lower()
        if resp == 'todos':
            ids = [int(item[0]) for item in items if str(item[0]).isdigit()]
        else:
            # Usar _es_respuesta_numerica_simple para evitar números falsos
            numero = _es_respuesta_numerica_simple(respuesta)
            if numero is not None and numero.is_integer():
                idx = int(numero) - 1
                if 0 <= idx < len(items):
                    ids = [int(items[idx][0])]
                else:
                    await update.message.reply_text("⚠️ Número fuera de rango. Intenta de nuevo.")
                    return
            else:
                # Intentar con comas
                numeros = re.findall(r'\b\d+\b', respuesta)
                if not numeros:
                    await update.message.reply_text("⚠️ No entendí. Escribe números separados por comas (ej: 1,3,5) o 'todos'.")
                    return
                indices = [int(n) for n in numeros if 1 <= int(n) <= len(items)]
                if not indices:
                    await update.message.reply_text("⚠️ Números fuera de rango. Intenta de nuevo.")
                    return
                ids = [int(items[i-1][0]) for i in indices if str(items[i-1][0]).isdigit()]

        if not ids:
            await update.message.reply_text("⚠️ No se pudieron extraer IDs válidos.")
            return

        context.user_data['borrar_ids'] = ids
        context.user_data['estado_espera'] = 'confirmar_borrado'

        count = len(ids)
        await update.message.reply_text(
            f"⚠️ ¿Seguro que quieres BORRAR DEFINITIVAMENTE los {count} elementos seleccionados?\n"
            "Esto no se deshace. Responde 'SÍ' para confirmar."
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error al procesar selección: {str(e)[:100]}")
        context.user_data['estado_espera'] = None

async def procesar_confirmacion_borrado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    respuesta = update.message.text
    try:
        ids = context.user_data.get('borrar_ids', [])
        tipo = context.user_data.get('borrar_tipo')
        if not ids or not tipo:
            await update.message.reply_text("⚠️ No tengo elementos en memoria, jefe.")
            context.user_data['estado_espera'] = None
            return

        if not _es_confirmacion(respuesta):
            await update.message.reply_text("✅ Borrado cancelado, jefe.")
            context.user_data['estado_espera'] = None
            context.user_data['borrar_ids'] = None
            context.user_data['borrar_items'] = None
            context.user_data['borrar_tipo'] = None
            return

        conn = get_db_connection()
        cur = conn.cursor()
        if tipo == 'gastos':
            borrados = borrar_gastos_por_ids(cur, ids)
            clientes_eliminados = 0
        else:
            borrados = borrar_proyectos_por_ids(cur, ids)
            clientes_eliminados = limpiar_clientes_huérfanos(cur)
        conn.commit()
        cur.close(); conn.close()

        mensaje = f"🗑️ {borrados} proyectos borrados definitivamente, jefe."
        if clientes_eliminados > 0:
            mensaje += f" 🧹 También se eliminaron {clientes_eliminados} clientes sin proyectos."
        await update.message.reply_text(mensaje)

        context.user_data['estado_espera'] = None
        context.user_data['borrar_ids'] = None
        context.user_data['borrar_items'] = None
        context.user_data['borrar_tipo'] = None
    except Exception as e:
        await update.message.reply_text(f"❌ Error al borrar: {str(e)[:100]}")
        context.user_data['estado_espera'] = None

async def procesar_tipo_borrado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    respuesta = update.message.text
    resp = respuesta.strip()
    if resp == '1':
        await iniciar_borrado(update, context, 'activos')
    elif resp == '2':
        await iniciar_borrado(update, context, 'cancelados')
    elif resp == '3':
        await iniciar_borrado(update, context, 'liquidados')
    elif resp == '4':
        await iniciar_borrado(update, context, 'gastos')
    else:
        await update.message.reply_text("⚠️ Responde 1, 2, 3 o 4.")

# ==================== PROCESADOR PRINCIPAL ====================
async def ejecutar_una_accion(datos: dict, update: Update, context: ContextTypes.DEFAULT_TYPE, cliente_activo: str) -> bool:
    accion = datos.get("accion", "preguntar")

    if accion == "preguntar":
        pregunta = datos.get("pregunta", "¿Puedes darme más detalles, jefe?")
        historial = context.user_data.get('historial', [])
        historial.append(f"🤖 Bot preguntó: {pregunta}")
        context.user_data['historial'] = historial
        await update.message.reply_text(f"🤔 {pregunta}")
        return True

    if accion == "consultar_presupuesto":
        nombre_cliente = datos.get("cliente", cliente_activo if cliente_activo else "")
        if not nombre_cliente or nombre_cliente == "Desconocido":
            await update.message.reply_text("⚠️ ¿De qué cliente quieres saber el presupuesto, jefe?")
            return True
        context.args = [nombre_cliente]
        await comando_presupuesto(update, context)
        return False

    if accion == "consultar_material":
        nombre_cliente = datos.get("cliente", cliente_activo if cliente_activo else "")
        if not nombre_cliente or nombre_cliente == "Desconocido":
            await update.message.reply_text("⚠️ ¿De qué cliente quieres saber el material, jefe?")
            return True
        context.args = [nombre_cliente]
        await comando_material(update, context)
        return False

    if accion == "consultar_historial":
        nombre_buscar = datos.get("cliente", cliente_activo)
        if nombre_buscar and nombre_buscar != "Desconocido":
            context.args = [nombre_buscar]
            await comando_historial(update, context)
        else:
            await update.message.reply_text("⚠️ ¿De qué cliente quieres el historial, jefe?")
        return False

    if accion == "consultar_gastos":
        await comando_gastos(update, context)
        return False

    if accion == "consultar":
        tipo = datos.get("tipo_consulta", "activos")
        cliente_filtro = datos.get("cliente", None)
        conn = get_db_connection()
        cur = conn.cursor()
        proyectos = consultar_proyectos(cur, tipo, cliente_filtro)
        cur.close(); conn.close()
        if not proyectos:
            if tipo == "cancelados":
                await update.message.reply_text("📭 No hay proyectos cancelados, jefe.")
            elif tipo == "liquidados":
                await update.message.reply_text("📭 No hay proyectos liquidados, jefe.")
            else:
                await update.message.reply_text("📭 No hay clientes activos, jefe.")
            return False
        if tipo == "cancelados":
            titulo = "🗑️ **PROYECTOS CANCELADOS**"
        elif tipo == "liquidados":
            titulo = "✅ **PROYECTOS LIQUIDADOS**"
        else:
            titulo = "📊 **CLIENTES ACTIVOS**"
        msg = f"{titulo}:\n\n"
        for n, nc, desc, t, p, e, pres_comp, mat_comp, tel, dir, notas in proyectos:
            pendiente = t - p
            pres_icon = "📋" if pres_comp else "⏳"
            mat_icon = "🛠️" if mat_comp else "❌"
            msg += f"👤 *{n}*\n"
            msg += f"   🔧 Proyecto: {nc or 'Proyecto General'}\n"
            msg += f"   📝 Detalle: {desc}\n"
            if tel:
                msg += f"   📞 Tel: {tel}\n"
            if dir:
                msg += f"   📍 Dirección: {dir}\n"
            if notas:
                msg += f"   📌 Notas: {notas}\n"
            msg += f"   💰 Total: ${t:.2f} | Pagado: ${p:.2f} | Saldo: ${pendiente:.2f}\n"
            msg += f"   📌 Estado: {e} | {pres_icon} Presupuesto | {mat_icon} Material\n\n"
        # Guardar lista para edición por número
        items_guardar = [(p[1] if len(p)>1 else None, p[0] if len(p)>0 else None, p[1] if len(p)>1 else None, p[2] if len(p)>2 else None, p[3] if len(p)>3 else None, p[5] if len(p)>5 else None) for p in proyectos]
        _guardar_ultima_lista(context, items_guardar, tipo="consulta")
        await update.message.reply_text(msg, parse_mode='Markdown')
        return False

    if accion == "iniciar_borrado":
        tipo = datos.get("tipo_borrado")
        cliente = datos.get("cliente")
        await iniciar_borrado(update, context, tipo, cliente)
        return True

    # ===== ACCIONES DE ESCRITURA =====
    cliente_nombre = datos.get("cliente", cliente_activo if cliente_activo else "Desconocido")
    nombre_corto = datos.get("nombre_corto", "Proyecto General")
    monto = float(datos.get("monto", 0) or 0)
    descripcion = datos.get("descripcion", "")
    estado = datos.get("estado", "Pendiente de cotizar")
    telefono = datos.get("telefono", "")
    direccion = datos.get("direccion", "")
    notas = datos.get("notas", "")
    resumen_ia = datos.get("resumen", "")

    if cliente_nombre != "Desconocido" and cliente_nombre != cliente_activo:
        context.user_data['cliente_activo'] = cliente_nombre.lower()

    # Verificar si hay un proyecto forzado por número de la última lista
    proyecto_id_forzado = context.user_data.get('proyecto_id_forzado')
    if proyecto_id_forzado:
        # Usamos ese ID para actualizar directamente
        pass

    conn = get_db_connection()
    cur = conn.cursor()

    # ===== FUSIONAR PROYECTOS =====
    if accion == "fusionar_proyectos":
        if not cliente_nombre or cliente_nombre == "Desconocido":
            cur.close(); conn.close()
            await update.message.reply_text("⚠️ ¿De qué cliente quieres fusionar proyectos, jefe?")
            return True
        proyectos = obtener_proyectos_activos(cur, cliente_nombre)
        cur.close(); conn.close()
        if len(proyectos) < 2:
            await update.message.reply_text(f"⚠️ {cliente_nombre} no tiene 2+ proyectos activos para fusionar, jefe.")
            return False
        msg = f"🔗 **{cliente_nombre}** tiene estos proyectos activos. ¿Cuáles fusiono?\n\n"
        for i, p in enumerate(proyectos, 1):
            msg += f"{i}. *{p[1] or 'Proyecto'}* - ${p[3]:.2f} ({p[5]})\n"
        msg += "\nResponde los números separados por comas (ej: 1,2) o 'todos'."
        context.user_data['estado_espera'] = 'seleccion_fusion'
        context.user_data['proyectos_fusion'] = proyectos
        context.user_data['cliente_fusion'] = cliente_nombre
        await update.message.reply_text(msg, parse_mode='Markdown')
        return True

    # ===== MARCAR PRESUPUESTO ENVIADO =====
    if accion == "marcar_presupuesto_enviado":
        marcado, candidatos = marcar_presupuesto_enviado(
            cur, cliente_nombre, nombre_corto,
            monto=monto if monto > 0 else None,
            descripcion=descripcion if descripcion else None,
            telefono=telefono,
            direccion=direccion,
            notas=notas
        )
        if marcado:
            conn.commit(); cur.close(); conn.close()
            msg = f"📋 Presupuesto marcado como ENVIADO para {cliente_nombre}, jefe."
            if monto > 0:
                msg += f" 💰 Monto actualizado a ${monto:.2f}."
            if descripcion:
                msg += f" 📝 Detalle actualizado."
            await update.message.reply_text(msg)
            return False
        elif candidatos:
            cur.close(); conn.close()
            msg = f"👤 **{cliente_nombre}** tiene varios proyectos activos. ¿A cuál le mandaste el presupuesto?\n\n"
            for i, c in enumerate(candidatos, 1):
                msg += f"{i}. *{c[1] or 'Proyecto'}*\n"
            msg += "\nResponde el número o el nombre del proyecto."
            context.user_data['estado_espera'] = 'seleccion_presupuesto'
            context.user_data['candidatos_presupuesto'] = candidatos
            context.user_data['cliente_presupuesto'] = cliente_nombre
            context.user_data['datos_presupuesto'] = {
                'monto': monto, 'descripcion': descripcion,
                'telefono': telefono, 'direccion': direccion, 'notas': notas
            }
            await update.message.reply_text(msg, parse_mode='Markdown')
            return True
        else:
            cur.close(); conn.close()
            await update.message.reply_text(f"⚠️ No encontré proyectos activos para {cliente_nombre}, jefe.")
            return False

    # ===== REGISTRAR PROYECTO =====
    if accion == "registrar_proyecto":
        buscar_o_crear_cliente(cur, cliente_nombre, telefono, direccion, notas)
        cliente_id = buscar_o_crear_cliente(cur, cliente_nombre)
        registrar_proyecto(cur, cliente_id, nombre_corto, descripcion or nombre_corto, monto, estado, notas)
        conn.commit(); cur.close(); conn.close()
        respuesta = f"✅ Nuevo proyecto guardado, jefe: {resumen_ia}\n Total: ${monto:.2f}"
        if monto == 0:
            respuesta += "\n\n📌 Recuerda que puedes actualizar el monto después o decir 'pendiente' si no lo tienes."
        await update.message.reply_text(respuesta)
        return False

    # ===== ACTUALIZAR PROYECTO =====
    if accion == "actualizar_proyecto":
        buscar_o_crear_cliente(cur, cliente_nombre, telefono, direccion, notas)
        actualizado, candidatos = actualizar_proyecto(cur, cliente_nombre, nombre_corto, descripcion, monto, estado, notas, proyecto_id_forced=proyecto_id_forzado)
        if proyecto_id_forzado:
            context.user_data['proyecto_id_forzado'] = None
        if actualizado:
            respuesta = f"✏️ Proyecto actualizado, patrón: {resumen_ia}"
            conn.commit(); cur.close(); conn.close()
            await update.message.reply_text(respuesta)
            return False
        elif candidatos:
            cur.close(); conn.close()
            msg = f"👤 **{cliente_nombre}** tiene varios proyectos activos. ¿Cuál quieres actualizar?\n\n"
            for i, c in enumerate(candidatos, 1):
                msg += f"{i}. *{c[1] or 'Proyecto'}*\n"
            msg += "\nResponde el número o el nombre del proyecto."
            context.user_data['estado_espera'] = 'seleccion_actualizar'
            context.user_data['candidatos_actualizar'] = candidatos
            context.user_data['cliente_actualizar'] = cliente_nombre
            context.user_data['datos_actualizar'] = {
                'nombre_corto': nombre_corto, 'descripcion': descripcion, 'monto': monto,
                'estado': estado, 'notas': notas
            }
            await update.message.reply_text(msg, parse_mode='Markdown')
            return True
        else:
            cur.close(); conn.close()
            await update.message.reply_text(f"⚠️ No encontré proyectos activos para {cliente_nombre}, jefe.")
            return False

    # ===== CANCELAR PROYECTO =====
    if accion == "cancelar_proyecto":
        proyectos = obtener_proyectos_activos(cur, cliente_nombre)
        cur.close(); conn.close()
        if not proyectos:
            await update.message.reply_text(f"⚠️ No encontré proyectos activos para {cliente_nombre}, jefe.")
            return False
        objetivo = (nombre_corto or descripcion or "").strip().lower()
        coincidencias = [p for p in proyectos if objetivo and objetivo not in ("proyecto general", "") and
                          ((p[1] and objetivo in p[1].lower()) or (p[2] and objetivo in p[2].lower()))]
        if len(proyectos) == 1 or len(coincidencias) == 1:
            pid = proyectos[0][0] if len(proyectos) == 1 else coincidencias[0][0]
            nc = proyectos[0][1] if len(proyectos) == 1 else coincidencias[0][1]
            conn = get_db_connection(); cur = conn.cursor()
            cur.execute("UPDATE proyectos SET estado = 'Cancelado' WHERE id = %s", (pid,))
            conn.commit(); cur.close(); conn.close()
            await update.message.reply_text(f"🗑️ Proyecto '{nc or 'Proyecto'}' de {cliente_nombre} CANCELADO, jefe.")
            return False
        else:
            msg = f"👤 **{cliente_nombre}** tiene varios proyectos. ¿Cuál quieres cancelar?\n\n"
            for i, (pid, nc, desc, total, pagado, estado, mat_comp, fecha_mat, costo_mat, pres_comp, fecha_pres) in enumerate(proyectos, 1):
                msg += f"{i}. *{nc or 'Proyecto'}* - {desc[:40]}...\n"
            msg += "\nResponde el número o el nombre."
            context.user_data['estado_espera'] = 'seleccion_cancelar'
            context.user_data['proyectos_cancelar'] = proyectos
            context.user_data['cliente_cancelar'] = cliente_nombre
            await update.message.reply_text(msg, parse_mode='Markdown')
            return True

    # ===== REGISTRAR PAGO =====
    if accion == "registrar_pago":
        buscar_o_crear_cliente(cur, cliente_nombre, telefono, direccion)
        proyecto_id, msg_pago, candidatos = registrar_pago(cur, cliente_nombre, monto, nombre_corto or descripcion)
        if proyecto_id:
            conn.commit(); cur.close(); conn.close()
            await update.message.reply_text(f"💰 Pago registrado: {resumen_ia}\n{msg_pago}")
            return False
        elif candidatos:
            cur.close(); conn.close()
            msg = f"👤 **{cliente_nombre}** tiene varios proyectos activos. ¿A cuál le aplico el pago?\n\n"
            for i, c in enumerate(candidatos, 1):
                msg += f"{i}. *{c[1] or 'Proyecto'}*\n"
            msg += "\nResponde el número o el nombre del proyecto."
            context.user_data['estado_espera'] = 'seleccion_pago'
            context.user_data['candidatos_pago'] = candidatos
            context.user_data['cliente_pago'] = cliente_nombre
            context.user_data['monto_pago'] = monto
            await update.message.reply_text(msg, parse_mode='Markdown')
            return True
        else:
            cur.close(); conn.close()
            await update.message.reply_text(f"⚠️ No encontré proyectos para {cliente_nombre}, jefe.")
            return False

    # ===== COMPRA DE MATERIAL =====
    if accion == "registrar_compra_material":
        if not cliente_nombre or cliente_nombre == "Desconocido":
            cur.close(); conn.close()
            await update.message.reply_text("⚠️ ¿Para qué cliente compraste el material, jefe?")
            return True
        proyectos = obtener_proyectos_activos(cur, cliente_nombre)
        cur.close(); conn.close()
        if not proyectos:
            await update.message.reply_text(f"⚠️ No tengo proyectos activos para {cliente_nombre}, jefe.")
            return False
        if len(proyectos) == 1:
            pid, nc = proyectos[0][0], proyectos[0][1]
            costo = monto
            conn = get_db_connection(); cur = conn.cursor()
            if costo == 0:
                context.user_data['estado_espera'] = 'esperando_costo_material'
                context.user_data['proyecto_id'] = pid
                context.user_data['cliente_nombre'] = cliente_nombre
                cur.close(); conn.close()
                await update.message.reply_text(f"🤔 ¿Quieres registrar el costo del material para '{nc}', jefe? Responde el monto (ej: 4500) o 'no' para saltarlo.")
                return True
            else:
                marcar_material_comprado(cur, pid, costo)
                conn.commit(); cur.close(); conn.close()
                await update.message.reply_text(f"✅ Material marcado como comprado para *{cliente_nombre} - {nc}*, jefe.\n💰 Costo: ${costo:.2f}")
                return False
        else:
            msg = f"👤 **{cliente_nombre}** tiene {len(proyectos)} proyectos:\n\n"
            for i, (pid, nc, desc, total, pagado, estado, mat_comp, fecha_mat, costo_mat, pres_comp, fecha_pres) in enumerate(proyectos, 1):
                msg += f"{i}. *{nc or 'Proyecto'}* - {desc[:40]}...\n"
            msg += "\n¿Para cuál proyecto o 'todos'?"
            context.user_data['estado_espera'] = 'seleccion_material'
            context.user_data['proyectos_seleccion'] = proyectos
            context.user_data['cliente_nombre'] = cliente_nombre
            context.user_data['costo_sugerido'] = monto
            await update.message.reply_text(msg, parse_mode='Markdown')
            return True

    # ===== GASTO GENERAL =====
    if accion == "registrar_gasto":
        registrar_gasto(cur, descripcion or resumen_ia, monto)
        conn.commit(); cur.close(); conn.close()
        await update.message.reply_text(f"🧾 Gasto registrado: {descripcion or resumen_ia} - ${monto:.2f}")
        return False

    await update.message.reply_text(f"⚠️ No sé cómo procesar '{accion}', jefe. ¿Puedes repetir?")
    return False

async def procesar_texto(update: Update, context: ContextTypes.DEFAULT_TYPE, texto_original: str):
    try:
        historial = context.user_data.get('historial', [])
        cliente_activo = context.user_data.get('cliente_activo', '')

        historial.append(f"👤 Jefe: {texto_original}")
        if len(historial) > 15:
            historial = historial[-15:]
        context.user_data['historial'] = historial

        # Verificar si hay un patrón de edición por número antes de llamar a la IA
        match = _PATRON_EDITAR_POR_NUMERO.search(texto_original.lower())
        if match:
            proyecto_id = _resolver_numero_de_ultima_lista(context, texto_original)
            if proyecto_id:
                context.user_data['proyecto_id_forzado'] = proyecto_id

        datos_completo = analizar_con_ia(texto_original, historial, cliente_activo, "")

        acciones = datos_completo.get("acciones")
        if not isinstance(acciones, list) or not acciones:
            acciones = [datos_completo]

        acciones = [_corregir_accion_con_texto(a, texto_original) for a in acciones]

        resumen_log = ", ".join(f"{a.get('accion','?')}" for a in acciones)
        historial.append(f"🤖 IA: [{resumen_log}] -> {datos_completo.get('resumen', '')}")
        context.user_data['historial'] = historial

        for datos in acciones:
            cliente_activo = context.user_data.get('cliente_activo', cliente_activo)
            detener = await ejecutar_una_accion(datos, update, context, cliente_activo)
            if detener:
                break

    except Exception as e:
        logging.error(f"🔴 Error: {e}")
        await update.message.reply_text(f"❌ Error interno: {str(e)[:150]}. Lo siento, jefe.")

# ==================== MANEJO DE MENSAJES ====================
async def manejar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text:
        texto = update.message.text
        estado = context.user_data.get('estado_espera')

        if estado and _es_escape(texto):
            for key in list(context.user_data.keys()):
                if key not in ('historial', 'cliente_activo', 'ultima_lista'):
                    context.user_data[key] = None
            context.user_data['estado_espera'] = None
            await update.message.reply_text("✅ Ok, lo dejo así, jefe. ¿En qué más le ayudo?")
            return

        if estado == 'esperando_tipo_borrado':
            await procesar_tipo_borrado(update, context)
        elif estado == 'esperando_seleccion_borrado':
            await procesar_seleccion_borrado(update, context)
        elif estado == 'confirmar_borrado':
            await procesar_confirmacion_borrado(update, context)
        elif estado == 'seleccion_material':
            await procesar_seleccion_material(update, context, texto)
        elif estado == 'esperando_costo_material':
            await procesar_costo_material(update, context, texto)
        elif estado == 'seleccion_cancelar':
            await procesar_seleccion_cancelar(update, context, texto)
        elif estado == 'seleccion_presupuesto':
            await procesar_seleccion_presupuesto(update, context, texto)
        elif estado == 'seleccion_actualizar':
            await procesar_seleccion_actualizar(update, context, texto)
        elif estado == 'seleccion_pago':
            await procesar_seleccion_pago(update, context, texto)
        elif estado == 'seleccion_fusion':
            await procesar_seleccion_fusion(update, context, texto)
        else:
            await procesar_texto(update, context, texto)
    elif update.message.voice:
        try:
            await update.message.reply_text("🎙️ Escuchando, jefe...")
            file = await context.bot.get_file(update.message.voice.file_id)
            ruta = 'voice.ogg'
            await file.download_to_drive(ruta)
            texto = transcribir_audio(ruta)
            os.remove(ruta)
            if len(texto.strip().split()) < 3:
                await update.message.reply_text("🎙️ No entendí bien el audio, ¿puedes repetirlo, jefe?")
                return
            await update.message.reply_text(f"📝 *\"{texto}\"*", parse_mode='Markdown')
            await procesar_texto(update, context, texto)
        except Exception as e:
            await update.message.reply_text(f"❌ Error de audio: {str(e)[:100]}. Disculpe, jefe.")

# ==================== FUNCIONES DE ESTADOS ====================
async def procesar_seleccion_material(update, context, respuesta):
    try:
        proyectos = context.user_data.get('proyectos_seleccion', [])
        cliente_nombre = context.user_data.get('cliente_nombre', '')
        costo_sugerido = context.user_data.get('costo_sugerido', 0)
        if not proyectos:
            await update.message.reply_text("⚠️ No tengo proyectos en memoria, jefe.")
            context.user_data['estado_espera'] = None
            return
        seleccion = None
        resp_lower = respuesta.lower().strip()
        if resp_lower in ['todos', 'todo', 'ambos', 'todas']:
            seleccion = 'todos'
        else:
            # Usar _es_respuesta_numerica_simple
            numero = _es_respuesta_numerica_simple(respuesta)
            if numero is not None and numero.is_integer():
                idx = int(numero) - 1
                if 0 <= idx < len(proyectos):
                    seleccion = idx + 1
            if seleccion is None:
                for i, (pid, nc, desc, total, pagado, estado, mat_comp, fecha_mat, costo_mat, pres_comp, fecha_pres) in enumerate(proyectos, 1):
                    if nc and nc.lower() in resp_lower:
                        seleccion = i
                        break
                    if desc and desc.lower() in resp_lower:
                        seleccion = i
                        break
        if seleccion is None:
            await update.message.reply_text("⚠️ No entendí, responde con el número, el nombre del proyecto o 'todos'.")
            return
        conn = get_db_connection()
        cur = conn.cursor()
        if seleccion == 'todos':
            for pid, nc, desc, total, pagado, estado, mat_comp, fecha_mat, costo_mat, pres_comp, fecha_pres in proyectos:
                marcar_material_comprado(cur, pid, costo_sugerido if costo_sugerido > 0 else None)
            conn.commit()
            cur.close(); conn.close()
            await update.message.reply_text(f"✅ Material marcado como comprado para **todos** los proyectos de {cliente_nombre}, jefe.")
        else:
            idx = seleccion - 1
            pid = proyectos[idx][0]
            nc = proyectos[idx][1]
            if costo_sugerido == 0:
                context.user_data['estado_espera'] = 'esperando_costo_material'
                context.user_data['proyecto_id'] = pid
                await update.message.reply_text(f"🤔 ¿Quieres registrar el costo del material para '{nc}', jefe? Responde el monto (ej: 4500) o 'no' para saltarlo.")
                return
            marcar_material_comprado(cur, pid, costo_sugerido)
            conn.commit()
            cur.close(); conn.close()
            await update.message.reply_text(f"✅ Material marcado como comprado para *{cliente_nombre} - {nc}*, jefe.\n💰 Costo: ${costo_sugerido:.2f}")
        context.user_data['estado_espera'] = None
        context.user_data['proyectos_seleccion'] = None
        context.user_data['cliente_nombre'] = None
        context.user_data['costo_sugerido'] = None
    except Exception as e:
        await update.message.reply_text(f"❌ Error en selección: {str(e)[:100]}")
        context.user_data['estado_espera'] = None

async def procesar_costo_material(update, context, respuesta):
    try:
        proyecto_id = context.user_data.get('proyecto_id')
        cliente_nombre = context.user_data.get('cliente_nombre', '')
        if not proyecto_id:
            await update.message.reply_text("⚠️ No tengo proyecto en memoria.")
            context.user_data['estado_espera'] = None
            return
        resp_lower = respuesta.lower().strip()
        if resp_lower.startswith('no'):
            conn = get_db_connection()
            cur = conn.cursor()
            marcar_material_comprado(cur, proyecto_id, None)
            conn.commit()
            cur.close(); conn.close()
            await update.message.reply_text("✅ Material marcado como comprado sin costo, jefe.")
        else:
            costo = _es_respuesta_numerica_simple(respuesta)
            if costo is not None:
                conn = get_db_connection()
                cur = conn.cursor()
                marcar_material_comprado(cur, proyecto_id, costo)
                conn.commit()
                cur.close(); conn.close()
                await update.message.reply_text(f"✅ Material marcado con costo de ${costo:.2f}, jefe.")
            else:
                # No es un número simple: probablemente el jefe cambió de tema
                await update.message.reply_text(
                    "🤔 Eso no es un monto. Voy a procesar tu mensaje como una nueva "
                    "instrucción, pero recuerda que sigo esperando el costo del "
                    "material — dímelo cuando puedas."
                )
                await procesar_texto(update, context, respuesta)
                return
        context.user_data['estado_espera'] = None
        context.user_data['proyecto_id'] = None
        context.user_data['cliente_nombre'] = None
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
        context.user_data['estado_espera'] = None

async def procesar_seleccion_cancelar(update, context, respuesta):
    try:
        proyectos = context.user_data.get('proyectos_cancelar', [])
        if not proyectos:
            await update.message.reply_text("⚠️ No tengo proyectos en memoria.")
            context.user_data['estado_espera'] = None
            return
        resp_lower = respuesta.lower().strip()
        seleccion = None
        numero = _es_respuesta_numerica_simple(respuesta)
        if numero is not None and numero.is_integer():
            idx = int(numero) - 1
            if 0 <= idx < len(proyectos):
                seleccion = idx + 1
        if seleccion is None:
            for i, (pid, nc, desc, total, pagado, estado, mat_comp, fecha_mat, costo_mat, pres_comp, fecha_pres) in enumerate(proyectos, 1):
                if nc and nc.lower() in resp_lower:
                    seleccion = i
                    break
                if desc and desc.lower() in resp_lower:
                    seleccion = i
                    break
        if seleccion is None:
            await update.message.reply_text("⚠️ Responde el número del proyecto o el nombre.")
            return
        idx = seleccion - 1
        pid = proyectos[idx][0]
        nc = proyectos[idx][1]
        cliente_nombre = context.user_data.get('cliente_cancelar', '')
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE proyectos SET estado = 'Cancelado' WHERE id = %s", (pid,))
        conn.commit()
        cur.close(); conn.close()
        await update.message.reply_text(f"🗑️ Proyecto '{nc}' de {cliente_nombre} CANCELADO, jefe.")
        context.user_data['estado_espera'] = None
        context.user_data['proyectos_cancelar'] = None
        context.user_data['cliente_cancelar'] = None
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
        context.user_data['estado_espera'] = None

async def procesar_seleccion_presupuesto(update, context, respuesta):
    try:
        candidatos = context.user_data.get('candidatos_presupuesto', [])
        cliente_nombre = context.user_data.get('cliente_presupuesto', '')
        datos = context.user_data.get('datos_presupuesto', {})
        if not candidatos:
            await update.message.reply_text("⚠️ No tengo proyectos en memoria, jefe.")
            context.user_data['estado_espera'] = None
            return
        pid = _elegir_candidato(respuesta, candidatos)
        if pid is None:
            await update.message.reply_text("⚠️ No entendí, responde con el número o el nombre del proyecto.")
            return
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            UPDATE proyectos 
            SET presupuesto_enviado = TRUE, 
                fecha_presupuesto = CURRENT_TIMESTAMP,
                estado = 'Presupuesto enviado',
                monto_total = CASE WHEN %s > 0 THEN %s ELSE monto_total END,
                descripcion = COALESCE(%s, descripcion),
                notas_adicionales = COALESCE(%s, notas_adicionales)
            WHERE id = %s
        """, (datos.get('monto', 0), datos.get('monto', 0), datos.get('descripcion'), datos.get('notas'), pid))
        if datos.get('telefono') or datos.get('direccion'):
            buscar_o_crear_cliente(cur, cliente_nombre, datos.get('telefono'), datos.get('direccion'))
        conn.commit()
        cur.close(); conn.close()
        await update.message.reply_text(f"📋 Presupuesto marcado como ENVIADO para {cliente_nombre}, jefe.")
        context.user_data['estado_espera'] = None
        context.user_data['candidatos_presupuesto'] = None
        context.user_data['cliente_presupuesto'] = None
        context.user_data['datos_presupuesto'] = None
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
        context.user_data['estado_espera'] = None

async def procesar_seleccion_actualizar(update, context, respuesta):
    try:
        candidatos = context.user_data.get('candidatos_actualizar', [])
        cliente_nombre = context.user_data.get('cliente_actualizar', '')
        datos = context.user_data.get('datos_actualizar', {})
        if not candidatos:
            await update.message.reply_text("⚠️ No tengo proyectos en memoria, jefe.")
            context.user_data['estado_espera'] = None
            return
        pid = _elegir_candidato(respuesta, candidatos)
        if pid is None:
            await update.message.reply_text("⚠️ No entendí, responde con el número o el nombre del proyecto.")
            return
        conn = get_db_connection()
        cur = conn.cursor()
        monto = float(datos.get('monto', 0) or 0)
        cur.execute("""
            UPDATE proyectos
            SET nombre_corto = COALESCE(%s, nombre_corto),
                descripcion = COALESCE(%s, descripcion),
                monto_total = CASE WHEN %s > 0 THEN %s ELSE monto_total END,
                estado = COALESCE(%s, estado),
                notas_adicionales = COALESCE(%s, notas_adicionales)
            WHERE id = %s
        """, (datos.get('nombre_corto') or None, datos.get('descripcion') or None, monto, monto,
              datos.get('estado') or None, datos.get('notas') or None, pid))
        conn.commit()
        cur.close(); conn.close()
        await update.message.reply_text(f"✏️ Proyecto de {cliente_nombre} actualizado, jefe.")
        context.user_data['estado_espera'] = None
        context.user_data['candidatos_actualizar'] = None
        context.user_data['cliente_actualizar'] = None
        context.user_data['datos_actualizar'] = None
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
        context.user_data['estado_espera'] = None

async def procesar_seleccion_pago(update, context, respuesta):
    try:
        candidatos = context.user_data.get('candidatos_pago', [])
        cliente_nombre = context.user_data.get('cliente_pago', '')
        monto = Decimal(str(context.user_data.get('monto_pago', 0)))
        if not candidatos:
            await update.message.reply_text("⚠️ No tengo proyectos en memoria, jefe.")
            context.user_data['estado_espera'] = None
            return
        pid = _elegir_candidato(respuesta, candidatos)
        if pid is None:
            await update.message.reply_text("⚠️ No entendí, responde con el número o el nombre del proyecto.")
            return
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT monto_total, monto_pagado, estado, nombre_corto FROM proyectos WHERE id = %s", (pid,))
        row = cur.fetchone()
        if not row:
            await update.message.reply_text("⚠️ No encontré el proyecto seleccionado.")
            context.user_data['estado_espera'] = None
            return
        monto_total, monto_pagado_actual, estado_actual, nc = row
        monto_total = Decimal(str(monto_total))
        monto_pagado_actual = Decimal(str(monto_pagado_actual))
        nuevo_pagado = monto_pagado_actual + monto
        saldo = max(Decimal('0'), monto_total - nuevo_pagado)
        nuevo_estado = "Liquidado" if saldo == 0 else "Por cobrar"
        cur.execute("UPDATE proyectos SET monto_pagado = %s, estado = %s WHERE id = %s",
                    (float(nuevo_pagado), nuevo_estado, pid))
        conn.commit(); cur.close(); conn.close()
        await update.message.reply_text(f"💰 Pago de ${float(monto):.2f} aplicado a '{nc}'. Saldo: ${float(saldo):.2f}. Estado: {nuevo_estado}")
        context.user_data['estado_espera'] = None
        context.user_data['candidatos_pago'] = None
        context.user_data['cliente_pago'] = None
        context.user_data['monto_pago'] = None
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
        context.user_data['estado_espera'] = None

async def procesar_seleccion_fusion(update, context, respuesta):
    try:
        proyectos = context.user_data.get('proyectos_fusion', [])
        cliente_nombre = context.user_data.get('cliente_fusion', '')
        if not proyectos:
            await update.message.reply_text("⚠️ No tengo proyectos en memoria, jefe.")
            context.user_data['estado_espera'] = None
            return
        resp = respuesta.strip().lower()
        if resp == 'todos':
            ids = [p[0] for p in proyectos]
        else:
            numeros = re.findall(r'\b\d+\b', respuesta)
            if not numeros:
                await update.message.reply_text("⚠️ No entendí. Escribe números separados por comas (ej: 1,2) o 'todos'.")
                return
            indices = [int(n) for n in numeros if 1 <= int(n) <= len(proyectos)]
            if not indices:
                await update.message.reply_text("⚠️ Números fuera de rango. Intenta de nuevo.")
                return
            ids = [proyectos[i-1][0] for i in indices if str(proyectos[i-1][0]).isdigit()]
        if len(ids) < 2:
            await update.message.reply_text("⚠️ Necesito al menos 2 proyectos para fusionar.")
            return
        # Mostrar vista previa y pedir confirmación
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT nombre_corto, descripcion, monto_total, monto_pagado, estado
            FROM proyectos WHERE id = ANY(%s)
        """, (ids,))
        filas = cur.fetchall()
        cur.close(); conn.close()
        if len(filas) < 2:
            await update.message.reply_text("⚠️ Algunos proyectos ya no existen.")
            return
        preview = "🔗 **VISTA PREVIA DE FUSIÓN:**\n\n"
        total_monto = sum(f[2] for f in filas)
        total_pagado = sum(f[3] for f in filas)
        for i, f in enumerate(filas, 1):
            preview += f"{i}. {f[0] or 'Proyecto'} - ${f[2]:.2f} ({f[4]})\n"
        preview += f"\n📊 Total: ${total_monto:.2f} | Pagado: ${total_pagado:.2f} | Saldo: ${total_monto - total_pagado:.2f}"
        preview += "\n\n⚠️ ¿Confirmas la fusión? Responde 'SÍ' para ejecutar."
        context.user_data['estado_espera'] = 'confirmar_fusion'
        context.user_data['ids_fusion'] = ids
        context.user_data['cliente_fusion'] = cliente_nombre
        await update.message.reply_text(preview, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
        context.user_data['estado_espera'] = None

async def procesar_confirmacion_fusion(update, context, respuesta):
    try:
        ids = context.user_data.get('ids_fusion', [])
        cliente_nombre = context.user_data.get('cliente_fusion', '')
        if not ids:
            await update.message.reply_text("⚠️ No tengo IDs en memoria, jefe.")
            context.user_data['estado_espera'] = None
            return
        if not _es_confirmacion(respuesta):
            await update.message.reply_text("✅ Fusión cancelada, jefe.")
            context.user_data['estado_espera'] = None
            context.user_data['ids_fusion'] = None
            context.user_data['cliente_fusion'] = None
            return
        conn = get_db_connection()
        cur = conn.cursor()
        proyecto_id, msg = fusionar_proyectos(cur, ids)
        if proyecto_id:
            conn.commit()
            cur.close(); conn.close()
            await update.message.reply_text(f"✅ {msg}")
        else:
            cur.close(); conn.close()
            await update.message.reply_text(f"⚠️ {msg}")
        context.user_data['estado_espera'] = None
        context.user_data['ids_fusion'] = None
        context.user_data['cliente_fusion'] = None
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
        context.user_data['estado_espera'] = None

def _elegir_candidato(respuesta, candidatos):
    resp_lower = respuesta.lower().strip()
    # Intentar primero como número simple
    numero = _es_respuesta_numerica_simple(respuesta)
    if numero is not None and numero.is_integer():
        idx = int(numero) - 1
        if 0 <= idx < len(candidatos):
            return candidatos[idx][0]
    # Luego por nombre
    for pid, nc in candidatos:
        if nc and nc.lower() in resp_lower:
            return pid
    # Finalmente buscar número en toda la frase
    numeros = re.findall(r'\b\d+\b', respuesta)
    if len(numeros) == 1:
        idx = int(numeros[0]) - 1
        if 0 <= idx < len(candidatos):
            return candidatos[idx][0]
    return None

# ==================== INICIO ====================
if __name__ == '__main__':
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cliente", comando_cliente))
    app.add_handler(CommandHandler("estadisticas", comando_estadisticas))
    app.add_handler(CommandHandler("historial", comando_historial))
    app.add_handler(CommandHandler("resumen", comando_resumen))
    app.add_handler(CommandHandler("material", comando_material))
    app.add_handler(CommandHandler("presupuesto", comando_presupuesto))
    app.add_handler(CommandHandler("gastos", comando_gastos))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_mensaje))
    app.add_handler(MessageHandler(filters.VOICE, manejar_mensaje))
    print("🤖 Bot CORREGIDO: zona horaria CDMX, todas las mejoras de Claude implementadas.")
    app.run_polling(drop_pending_updates=True)
