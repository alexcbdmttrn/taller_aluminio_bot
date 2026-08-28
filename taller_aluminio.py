import os
import json
import re
import logging
import psycopg2
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

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

# ==================== INTELIGENCIA ARTIFICIAL ====================
def analizar_con_ia(texto, historial_mensajes, cliente_activo="", proyectos_existentes=""):
    contexto_historial = ""
    if historial_mensajes:
        contexto_historial = "[Historial de los últimos mensajes]:\n"
        for msg in historial_mensajes[-10:]:
            contexto_historial += f"- {msg}\n"
    
    # Detectar confirmación
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

REGLAS ESTRICTAS (LEE CON ATENCIÓN):
1. **MÚLTIPLES TAREAS EN UN MENSAJE**: El jefe puede darte varias instrucciones en una sola frase. Ej: "gasté 5000 y en presupuesto pon 10000" → Debes registrar AMBOS: material comprado con costo 5000 Y actualizar presupuesto a 10000. Extrae TODOS los datos que puedas.
2. **FALTAS DE ORTOGRAFÍA**: Si el jefe escribe mal ("presupuestoa" en vez de "presupuesto a"), INFIERE la palabra correcta por contexto.
3. **PREGUNTAR SI HAY DUDA**: Si no estás 100% seguro de lo que quiere, usa accion: "preguntar".
4. **CONFIRMACIONES**: "si", "sí", "esta bien", "ok", "okey" → NO crees nuevo proyecto, solo confirma lo anterior.
5. **NUEVO PROYECTO**: "registra", "anota", "nuevo" + cliente + trabajo.
6. **PRESUPUESTO**: Si dice "presupuesto", "cotización", "presupuestar" y menciona un monto → accion: "actualizar_proyecto" con estado "Presupuesto enviado" y actualiza monto_total. Si menciona que YA ENVIÓ presupuesto (sin monto) → accion: "marcar_presupuesto_enviado".
7. **COMPRA DE MATERIAL**: "compré material", "ya compré" + cliente. Si menciona monto, lo guarda. Si no, pregunta.
8. **CONSULTA DE PRESUPUESTO**: Si pregunta "¿ya se entregó presupuesto a [cliente]?" → accion: "consultar_presupuesto" con ese cliente.
9. **CONSULTA DE MATERIAL**: "¿ya compré material?", "¿material comprado?" + cliente → accion: "consultar_material".
10. **CONSULTAS GENERALES**: Solo si pregunta explícitamente "qué clientes tengo", "muestrame los clientes", "resumen" → accion: "consultar".
11. **CANCELAR**: "cancela", "cancelar", "ya no" + nombre específico del proyecto. NUNCA canceles todos sin preguntar. Si dice solo "cancela a [cliente]", pregunta cuál proyecto.
12. **BORRAR DEFINITIVAMENTE**: "borra definitivamente", "elimina del sistema" + cliente o proyecto. Si dice "todos", borra todos. Pide confirmación con "SÍ" (mayúscula o minúscula).

Responde SOLO con JSON:
{{
  "accion": "registrar_proyecto" | "registrar_pago" | "actualizar_proyecto" | "marcar_presupuesto_enviado" | "cancelar_proyecto" | "borrar_proyecto_definitivo" | "consultar" | "consultar_historial" | "consultar_material" | "consultar_presupuesto" | "registrar_compra_material" | "preguntar",
  "cliente": "nombre",
  "nombre_corto": "nombre breve del proyecto",
  "monto": numero o 0,
  "descripcion": "detalles",
  "notas": "",
  "estado": "Pendiente de cotizar" | "Presupuesto enviado" | "Aceptado" | "En proceso" | "Por cobrar" | "Liquidado" | "Cancelado",
  "tipo_consulta": "todos" | "cliente_especifico",
  "pregunta": "texto de la pregunta",
  "resumen": "frase corta de lo que harás"
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
        return json.loads(texto_limpio)
    except json.JSONDecodeError:
        return {"accion": "preguntar", "pregunta": "No entendí bien, ¿puedes repetirlo, jefe?"}

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
    cur.execute("SELECT id FROM clientes WHERE nombre ILIKE %s", (f"%{nombre_cliente}%",))
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

def obtener_proyectos_activos(cur, cliente_nombre):
    cur.execute("""
        SELECT p.id, p.nombre_corto, p.descripcion, p.monto_total, p.monto_pagado, p.estado,
               p.material_comprado, p.fecha_compra_material, p.costo_material,
               p.presupuesto_enviado, p.fecha_presupuesto
        FROM proyectos p 
        JOIN clientes c ON p.cliente_id = c.id 
        WHERE c.nombre ILIKE %s AND p.estado NOT IN ('Liquidado', 'Cancelado')
        ORDER BY p.fecha_creacion DESC
    """, (f"%{cliente_nombre}%",))
    return cur.fetchall()

def obtener_todos_proyectos_activos(cur):
    cur.execute("""
        SELECT c.nombre, p.id, p.nombre_corto, p.descripcion, p.monto_total, p.monto_pagado, p.estado,
               p.material_comprado, p.fecha_compra_material, p.costo_material,
               p.presupuesto_enviado, p.fecha_presupuesto
        FROM proyectos p 
        JOIN clientes c ON p.cliente_id = c.id 
        WHERE p.estado NOT IN ('Liquidado', 'Cancelado')
        ORDER BY c.nombre, p.fecha_creacion DESC
    """)
    return cur.fetchall()

def obtener_historial_cliente(cur, cliente_nombre):
    cur.execute("""
        SELECT p.nombre_corto, p.descripcion, p.monto_total, p.monto_pagado, p.estado, p.fecha_creacion,
               p.material_comprado, p.fecha_compra_material, p.costo_material,
               p.presupuesto_enviado, p.fecha_presupuesto
        FROM proyectos p 
        JOIN clientes c ON p.cliente_id = c.id 
        WHERE c.nombre ILIKE %s
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

def actualizar_proyecto(cur, cliente_nombre, nombre_corto, descripcion, monto, estado, notas="", presupuesto_enviado=None):
    cur.execute("""
        SELECT p.id FROM proyectos p
        JOIN clientes c ON p.cliente_id = c.id
        WHERE c.nombre ILIKE %s AND (p.nombre_corto ILIKE %s OR p.estado NOT IN ('Liquidado', 'Cancelado'))
        ORDER BY p.fecha_creacion DESC LIMIT 1
    """, (f"%{cliente_nombre}%", f"%{nombre_corto}%"))
    proyecto = cur.fetchone()
    if proyecto:
        updates = ["nombre_corto = %s", "descripcion = %s", "monto_total = %s", "estado = %s", "notas_adicionales = COALESCE(%s, notas_adicionales)"]
        params = [nombre_corto or 'Proyecto General', descripcion, monto, estado, notas or None]
        if presupuesto_enviado is not None:
            updates.append("presupuesto_enviado = %s")
            params.append(presupuesto_enviado)
            if presupuesto_enviado:
                updates.append("fecha_presupuesto = CURRENT_TIMESTAMP")
        params.append(proyecto[0])
        cur.execute(f"""
            UPDATE proyectos 
            SET {", ".join(updates)}
            WHERE id = %s
        """, tuple(params))
        return True
    return False

def marcar_presupuesto_enviado(cur, cliente_nombre, nombre_corto):
    cur.execute("""
        SELECT p.id FROM proyectos p
        JOIN clientes c ON p.cliente_id = c.id
        WHERE c.nombre ILIKE %s AND (p.nombre_corto ILIKE %s OR p.estado NOT IN ('Liquidado', 'Cancelado'))
        ORDER BY p.fecha_creacion DESC LIMIT 1
    """, (f"%{cliente_nombre}%", f"%{nombre_corto}%"))
    proyecto = cur.fetchone()
    if proyecto:
        cur.execute("""
            UPDATE proyectos 
            SET presupuesto_enviado = TRUE, fecha_presupuesto = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (proyecto[0],))
        return True
    return False

def cancelar_proyecto_especifico(cur, cliente_nombre, nombre_corto):
    cur.execute("""
        SELECT p.id FROM proyectos p
        JOIN clientes c ON p.cliente_id = c.id
        WHERE c.nombre ILIKE %s AND (p.nombre_corto ILIKE %s OR p.descripcion ILIKE %s)
        AND p.estado NOT IN ('Liquidado', 'Cancelado')
        ORDER BY p.fecha_creacion DESC LIMIT 1
    """, (f"%{cliente_nombre}%", f"%{nombre_corto}%", f"%{nombre_corto}%"))
    proyecto = cur.fetchone()
    if proyecto:
        cur.execute("UPDATE proyectos SET estado = 'Cancelado' WHERE id = %s", (proyecto[0],))
        return True
    return False

def cancelar_proyectos_todos(cur, cliente_nombre):
    cur.execute("""
        UPDATE proyectos 
        SET estado = 'Cancelado' 
        FROM clientes c 
        WHERE proyectos.cliente_id = c.id 
        AND c.nombre ILIKE %s 
        AND proyectos.estado NOT IN ('Liquidado', 'Cancelado')
    """, (f"%{cliente_nombre}%",))
    return cur.rowcount > 0

def borrar_proyecto_definitivo(cur, proyecto_id):
    cur.execute("DELETE FROM proyectos WHERE id = %s", (proyecto_id,))
    return cur.rowcount > 0

def borrar_todos_proyectos_cliente(cur, cliente_nombre):
    cur.execute("""
        DELETE FROM proyectos 
        USING clientes c 
        WHERE proyectos.cliente_id = c.id AND c.nombre ILIKE %s
    """, (f"%{cliente_nombre}%",))
    return cur.rowcount > 0

def registrar_pago(cur, cliente_nombre, monto_pago):
    cur.execute("""SELECT p.id, p.monto_total, p.monto_pagado, p.estado, p.nombre_corto
                   FROM proyectos p JOIN clientes c ON p.cliente_id = c.id
                   WHERE c.nombre ILIKE %s AND p.estado NOT IN ('Liquidado', 'Cancelado')
                   ORDER BY p.fecha_creacion DESC LIMIT 1""", (f"%{cliente_nombre}%",))
    proyecto = cur.fetchone()
    if not proyecto:
        return None, "No encontré proyectos pendientes para este cliente."
    
    proyecto_id, monto_total, monto_pagado, estado_actual, nombre_corto = proyecto
    nuevo_pagado = monto_pagado + monto_pago
    saldo = max(0, monto_total - nuevo_pagado)
    nuevo_estado = "Liquidado" if saldo == 0 else "Por cobrar"
    if monto_pago > 0 and estado_actual == "Pendiente de cotizar":
        nuevo_estado = "En proceso"
        
    cur.execute("UPDATE proyectos SET monto_pagado = %s, estado = %s WHERE id = %s", 
                (nuevo_pagado, nuevo_estado, proyecto_id))
    return proyecto_id, f"{nombre_corto}: Anticipo/Pago ${monto_pago:.2f}. Saldo: ${saldo:.2f}. Estado: {nuevo_estado}"

def marcar_material_comprado(cur, proyecto_id, costo=None):
    cur.execute("""
        UPDATE proyectos 
        SET material_comprado = TRUE, 
            fecha_compra_material = CURRENT_TIMESTAMP,
            costo_material = COALESCE(%s, costo_material)
        WHERE id = %s
    """, (costo, proyecto_id))
    return cur.rowcount > 0

def consultar_material_cliente(cur, cliente_nombre):
    cur.execute("""
        SELECT p.nombre_corto, p.descripcion, p.material_comprado, p.fecha_compra_material, p.costo_material
        FROM proyectos p 
        JOIN clientes c ON p.cliente_id = c.id 
        WHERE c.nombre ILIKE %s AND p.estado NOT IN ('Liquidado', 'Cancelado')
        ORDER BY p.fecha_creacion DESC
    """, (f"%{cliente_nombre}%",))
    return cur.fetchall()

def consultar_presupuesto_cliente(cur, cliente_nombre):
    cur.execute("""
        SELECT p.nombre_corto, p.descripcion, p.presupuesto_enviado, p.fecha_presupuesto, p.monto_total
        FROM proyectos p 
        JOIN clientes c ON p.cliente_id = c.id 
        WHERE c.nombre ILIKE %s AND p.estado NOT IN ('Liquidado', 'Cancelado')
        ORDER BY p.fecha_creacion DESC
    """, (f"%{cliente_nombre}%",))
    return cur.fetchall()

def consultar_proyectos(cur, tipo, cliente_nombre=None):
    if cliente_nombre:
        if tipo == "deudores":
            cur.execute("""
                SELECT c.nombre, p.nombre_corto, p.descripcion, p.monto_total, p.monto_pagado, 
                       (p.monto_total - p.monto_pagado) as saldo
                FROM proyectos p JOIN clientes c ON p.cliente_id = c.id
                WHERE c.nombre ILIKE %s AND p.estado IN ('Por cobrar', 'En proceso') AND p.monto_total > p.monto_pagado 
                ORDER BY saldo DESC
            """, (f"%{cliente_nombre}%",))
        else:
            cur.execute("""
                SELECT c.nombre, p.nombre_corto, p.descripcion, p.monto_total, p.monto_pagado, p.estado,
                       p.presupuesto_enviado, p.material_comprado
                FROM proyectos p JOIN clientes c ON p.cliente_id = c.id 
                WHERE c.nombre ILIKE %s AND p.estado NOT IN ('Liquidado', 'Cancelado')
                ORDER BY p.fecha_creacion DESC
            """, (f"%{cliente_nombre}%",))
    else:
        if tipo == "deudores":
            cur.execute("""
                SELECT c.nombre, p.nombre_corto, p.descripcion, p.monto_total, p.monto_pagado, 
                       (p.monto_total - p.monto_pagado) as saldo
                FROM proyectos p JOIN clientes c ON p.cliente_id = c.id
                WHERE p.estado IN ('Por cobrar', 'En proceso') AND p.monto_total > p.monto_pagado 
                ORDER BY saldo DESC
            """)
        elif tipo == "pendientes":
            cur.execute("""
                SELECT c.nombre, p.nombre_corto, p.descripcion, p.monto_total 
                FROM proyectos p JOIN clientes c ON p.cliente_id = c.id 
                WHERE p.estado = 'Pendiente de cotizar'
            """)
        elif tipo == "liquidados":
            cur.execute("""
                SELECT c.nombre, p.nombre_corto, p.descripcion, p.monto_total 
                FROM proyectos p JOIN clientes c ON p.cliente_id = c.id 
                WHERE p.estado = 'Liquidado' LIMIT 10
            """)
        else:  # "todos"
            cur.execute("""
                SELECT c.nombre, p.nombre_corto, p.descripcion, p.monto_total, p.monto_pagado, p.estado,
                       p.presupuesto_enviado, p.material_comprado
                FROM proyectos p JOIN clientes c ON p.cliente_id = c.id 
                WHERE p.estado NOT IN ('Liquidado', 'Cancelado')
                ORDER BY c.nombre, p.fecha_creacion DESC
            """)
    return cur.fetchall()

# ==================== COMANDOS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['historial'] = []
    context.user_data['cliente_activo'] = ""
    await update.message.reply_text(
        "¡Hola, jefe! 🛠️ Su secretario está listo.\n\n"
        "Comandos:\n"
        "/cliente [nombre] - Forzar cliente activo\n"
        "/estadisticas - Resumen de proyectos\n"
        "/historial [nombre] - Historial de un cliente\n"
        "/resumen - Ver clientes activos\n"
        "/material [nombre] - Consultar material comprado\n"
        "/presupuesto [nombre] - Consultar presupuestos enviados\n"
        "Puede decir 'borra definitivamente [cliente]' para eliminar un proyecto."
    )

async def comando_cliente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        cliente_nombre = " ".join(context.args)
        context.user_data['cliente_activo'] = cliente_nombre
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
    nombre = " ".join(context.args)
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT nombre, telefono, direccion FROM clientes WHERE nombre ILIKE %s", (f"%{nombre}%",))
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
                msg += f"{icono} *{nc or 'Proyecto'}* ({estado})\n   {desc[:60]}...\n"
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
        proyectos = consultar_proyectos(cur, "todos")
        cur.close(); conn.close()
        if not proyectos:
            await update.message.reply_text("📭 No hay clientes activos, jefe.")
            return
        msg = "📊 **CLIENTES ACTIVOS:**\n\n"
        for n, nc, desc, t, p, e, pres_comp, mat_comp in proyectos:
            pendiente = t - p
            pres_icon = "📋" if pres_comp else "⏳"
            mat_icon = "🛠️" if mat_comp else "❌"
            msg += f"👤 *{n}*\n   Proyecto: {nc or 'Proyecto General'}\n   Detalle: {desc[:80]}...\n"
            msg += f"   Total: ${t:.2f} | Pagado: ${p:.2f} | Saldo: ${pendiente:.2f} | {e} | {pres_icon} Presupuesto | {mat_icon} Material\n\n"
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def comando_material(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usa: /material Nombre, jefe. Ej: /material Pedro")
        return
    nombre = " ".join(context.args)
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
                msg += f"✅ *{nc or 'Proyecto'}*: Comprado el {fecha_str}{costo_str}\n   {desc[:60]}...\n\n"
            else:
                msg += f"❌ *{nc or 'Proyecto'}*: **No comprado aún**\n   {desc[:60]}...\n\n"
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def comando_presupuesto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usa: /presupuesto Nombre, jefe. Ej: /presupuesto Pedro")
        return
    nombre = " ".join(context.args)
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
                msg += f"✅ *{nc or 'Proyecto'}*: Enviado el {fecha_str} (${monto:.2f})\n   {desc[:60]}...\n\n"
            else:
                msg += f"❌ *{nc or 'Proyecto'}*: **No enviado aún**\n   {desc[:60]}...\n\n"
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

# ==================== MANEJO DE ESTADOS DE ESPERA ====================
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
            # Intentar extraer número
            try:
                num = int(resp_lower)
                if 1 <= num <= len(proyectos):
                    seleccion = num
            except ValueError:
                pass
            # Si no, buscar por nombre
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
            # Intentar extraer número de la respuesta
            numeros = re.findall(r'\d+', respuesta)
            if numeros:
                costo = float(numeros[0])
                conn = get_db_connection()
                cur = conn.cursor()
                marcar_material_comprado(cur, proyecto_id, costo)
                conn.commit()
                cur.close(); conn.close()
                await update.message.reply_text(f"✅ Material marcado con costo de ${costo:.2f}, jefe.")
            else:
                await update.message.reply_text("🤔 No entendí. Responde el monto (ej: 4500) o 'no' para saltarlo.")
                return
        
        context.user_data['estado_espera'] = None
        context.user_data['proyecto_id'] = None
        context.user_data['cliente_nombre'] = None
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
        context.user_data['estado_espera'] = None

async def procesar_seleccion_borrado(update, context, respuesta):
    try:
        proyectos = context.user_data.get('proyectos_borrar', [])
        if not proyectos:
            await update.message.reply_text("⚠️ No tengo proyectos en memoria.")
            context.user_data['estado_espera'] = None
            return
        resp_lower = respuesta.lower().strip()
        seleccion = None
        if resp_lower in ['todos', 'todo', 'ambos', 'todas']:
            seleccion = 'todos'
        else:
            try:
                num = int(resp_lower)
                if 1 <= num <= len(proyectos):
                    seleccion = num
            except ValueError:
                pass
            if seleccion is None:
                for i, (pid, nc, desc, total, pagado, estado, mat_comp, fecha_mat, costo_mat, pres_comp, fecha_pres) in enumerate(proyectos, 1):
                    if nc and nc.lower() in resp_lower:
                        seleccion = i
                        break
                    if desc and desc.lower() in resp_lower:
                        seleccion = i
                        break
        if seleccion is None:
            await update.message.reply_text("⚠️ Responde el número del proyecto, el nombre, o 'todos'.")
            return
        
        cliente_nombre = context.user_data.get('cliente_borrar', '')
        if seleccion == 'todos':
            context.user_data['estado_espera'] = 'confirmar_borrado_todos'
            context.user_data['cliente_borrar_todos'] = cliente_nombre
            await update.message.reply_text(f"⚠️ **¿Seguro que quieres BORRAR DEFINITIVAMENTE TODOS los proyectos de {cliente_nombre}?**\nEsto no se deshace. Responde 'SÍ' para confirmar.", parse_mode='Markdown')
        else:
            idx = seleccion - 1
            pid = proyectos[idx][0]
            nc = proyectos[idx][1]
            context.user_data['estado_espera'] = 'confirmar_borrado'
            context.user_data['proyecto_borrar'] = pid
            context.user_data['cliente_borrar'] = cliente_nombre
            await update.message.reply_text(f"⚠️ **¿Seguro que quieres BORRAR DEFINITIVAMENTE '{nc or 'Proyecto'}' de {cliente_nombre}?**\nEsto no se deshace. Responde 'SÍ' para confirmar.", parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
        context.user_data['estado_espera'] = None

async def procesar_confirmacion_borrado(update, context, respuesta):
    try:
        proyecto_id = context.user_data.get('proyecto_borrar')
        cliente_nombre = context.user_data.get('cliente_borrar', '')
        if not proyecto_id:
            await update.message.reply_text("⚠️ No tengo proyecto en memoria.")
            context.user_data['estado_espera'] = None
            return
        if respuesta.strip().upper() in ['SÍ', 'SI']:
            conn = get_db_connection()
            cur = conn.cursor()
            borrar_proyecto_definitivo(cur, proyecto_id)
            conn.commit()
            cur.close(); conn.close()
            await update.message.reply_text(f"🗑️ Proyecto **borrado definitivamente** de {cliente_nombre}, jefe.", parse_mode='Markdown')
        else:
            await update.message.reply_text("✅ Borrado cancelado, jefe.")
        context.user_data['estado_espera'] = None
        context.user_data['proyecto_borrar'] = None
        context.user_data['cliente_borrar'] = None
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
        context.user_data['estado_espera'] = None

async def procesar_confirmacion_borrado_todos(update, context, respuesta):
    try:
        cliente_nombre = context.user_data.get('cliente_borrar_todos', '')
        if not cliente_nombre:
            await update.message.reply_text("⚠️ No tengo cliente en memoria.")
            context.user_data['estado_espera'] = None
            return
        if respuesta.strip().upper() in ['SÍ', 'SI']:
            conn = get_db_connection()
            cur = conn.cursor()
            borrar_todos_proyectos_cliente(cur, cliente_nombre)
            conn.commit()
            cur.close(); conn.close()
            await update.message.reply_text(f"🗑️ **TODOS los proyectos de {cliente_nombre} borrados definitivamente**, jefe.", parse_mode='Markdown')
        else:
            await update.message.reply_text("✅ Borrado cancelado, jefe.")
        context.user_data['estado_espera'] = None
        context.user_data['cliente_borrar_todos'] = None
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
        try:
            num = int(resp_lower)
            if 1 <= num <= len(proyectos):
                seleccion = num
        except ValueError:
            pass
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

# ==================== PROCESADOR PRINCIPAL ====================
async def procesar_texto(update: Update, context: ContextTypes.DEFAULT_TYPE, texto_original: str):
    try:
        historial = context.user_data.get('historial', [])
        cliente_activo = context.user_data.get('cliente_activo', '')
        
        historial.append(f"👤 Jefe: {texto_original}")
        if len(historial) > 15:
            historial = historial[-15:]
        context.user_data['historial'] = historial
        
        datos = analizar_con_ia(texto_original, historial, cliente_activo, "")
        accion = datos.get("accion", "preguntar")
        
        historial.append(f"🤖 IA: {accion} -> {datos.get('resumen', '')}")
        context.user_data['historial'] = historial
        
        # ===== PREGUNTAR =====
        if accion == "preguntar":
            pregunta = datos.get("pregunta", "¿Puedes darme más detalles, jefe?")
            historial.append(f"🤖 Bot preguntó: {pregunta}")
            context.user_data['historial'] = historial
            await update.message.reply_text(f"🤔 {pregunta}")
            return

        # ===== CONSULTAR PRESUPUESTO =====
        if accion == "consultar_presupuesto":
            nombre_cliente = datos.get("cliente", cliente_activo if cliente_activo else "")
            if not nombre_cliente or nombre_cliente == "Desconocido":
                await update.message.reply_text("⚠️ ¿De qué cliente quieres saber el presupuesto, jefe?")
                return
            context.args = [nombre_cliente]
            await comando_presupuesto(update, context)
            return

        # ===== CONSULTAR MATERIAL =====
        if accion == "consultar_material":
            nombre_cliente = datos.get("cliente", cliente_activo if cliente_activo else "")
            if not nombre_cliente or nombre_cliente == "Desconocido":
                await update.message.reply_text("⚠️ ¿De qué cliente quieres saber el material, jefe?")
                return
            context.args = [nombre_cliente]
            await comando_material(update, context)
            return

        # ===== CONSULTAR HISTORIAL =====
        if accion == "consultar_historial":
            nombre_buscar = datos.get("cliente", cliente_activo)
            if nombre_buscar and nombre_buscar != "Desconocido":
                context.args = [nombre_buscar]
                await comando_historial(update, context)
            else:
                await update.message.reply_text("⚠️ ¿De qué cliente quieres el historial, jefe?")
            return

        # ===== CONSULTAR (resumen general) =====
        if accion == "consultar":
            conn = get_db_connection()
            cur = conn.cursor()
            proyectos = consultar_proyectos(cur, "todos")
            cur.close(); conn.close()
            if not proyectos:
                await update.message.reply_text("📭 No hay clientes activos, jefe.")
                return
            msg = "📊 **CLIENTES ACTIVOS:**\n\n"
            for n, nc, desc, t, p, e, pres_comp, mat_comp in proyectos:
                pendiente = t - p
                pres_icon = "📋" if pres_comp else "⏳"
                mat_icon = "🛠️" if mat_comp else "❌"
                msg += f"👤 *{n}*\n   Proyecto: {nc or 'Proyecto General'}\n   Detalle: {desc[:60]}...\n"
                msg += f"   Total: ${t:.2f} | Pagado: ${p:.2f} | Saldo: ${pendiente:.2f} | {e} | {pres_icon} Presupuesto | {mat_icon} Material\n\n"
            await update.message.reply_text(msg, parse_mode='Markdown')
            return

        # ===== ACCIONES DE ESCRITURA =====
        cliente_nombre = datos.get("cliente", cliente_activo if cliente_activo else "Desconocido")
        nombre_corto = datos.get("nombre_corto", "Proyecto General")
        monto = float(datos.get("monto", 0))
        descripcion = datos.get("descripcion", texto_original)
        estado = datos.get("estado", "Pendiente de cotizar")
        telefono = datos.get("telefono", "")
        direccion = datos.get("direccion", "")
        notas = datos.get("notas", "")
        resumen_ia = datos.get("resumen", "")

        if cliente_nombre != "Desconocido" and cliente_nombre != cliente_activo:
            context.user_data['cliente_activo'] = cliente_nombre

        conn = get_db_connection()
        cur = conn.cursor()

        # ===== MARCAR PRESUPUESTO ENVIADO =====
        if accion == "marcar_presupuesto_enviado":
            marcado = marcar_presupuesto_enviado(cur, cliente_nombre, nombre_corto)
            if marcado:
                respuesta = f"📋 **Presupuesto marcado como ENVIADO para {cliente_nombre} - {nombre_corto}, jefe.**"
            else:
                # Si no encuentra por nombre_corto, buscar por descripción
                marcado = marcar_presupuesto_enviado(cur, cliente_nombre, descripcion)
                if marcado:
                    respuesta = f"📋 **Presupuesto marcado como ENVIADO para {cliente_nombre}, jefe.**"
                else:
                    respuesta = f"⚠️ No encontré proyectos activos para {cliente_nombre}."
            conn.commit()
            cur.close(); conn.close()
            await update.message.reply_text(respuesta, parse_mode='Markdown')
            return

        # ===== REGISTRAR PROYECTO =====
        if accion == "registrar_proyecto":
            buscar_o_crear_cliente(cur, cliente_nombre, telefono, direccion, notas)
            cliente_id = buscar_o_crear_cliente(cur, cliente_nombre)
            registrar_proyecto(cur, cliente_id, nombre_corto, descripcion, monto, estado, notas)
            respuesta = f"✅ **Nuevo proyecto guardado, jefe:**\n{resumen_ia}\n Total: ${monto:.2f}"

        # ===== ACTUALIZAR PROYECTO =====
        elif accion == "actualizar_proyecto":
            buscar_o_crear_cliente(cur, cliente_nombre, telefono, direccion, notas)
            actualizado = actualizar_proyecto(cur, cliente_nombre, nombre_corto, descripcion, monto, estado, notas)
            if actualizado:
                respuesta = f"✏️ **Proyecto actualizado, patrón:**\n{resumen_ia}"
            else:
                respuesta = f"⚠️ No encontré proyectos activos para {cliente_nombre}, jefe."

        # ===== CANCELAR PROYECTO (específico) =====
        elif accion == "cancelar_proyecto":
            # Obtener proyectos del cliente
            proyectos = obtener_proyectos_activos(cur, cliente_nombre)
            cur.close(); conn.close()
            if not proyectos:
                await update.message.reply_text(f"⚠️ No encontré proyectos activos para {cliente_nombre}, jefe.")
                return
            if len(proyectos) == 1:
                pid, nc, desc, total, pagado, estado, mat_comp, fecha_mat, costo_mat, pres_comp, fecha_pres = proyectos[0]
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute("UPDATE proyectos SET estado = 'Cancelado' WHERE id = %s", (pid,))
                conn.commit()
                cur.close(); conn.close()
                await update.message.reply_text(f"🗑️ Proyecto '{nc or 'Proyecto'}' de {cliente_nombre} CANCELADO, jefe.")
            else:
                msg = f"👤 **{cliente_nombre}** tiene varios proyectos. ¿Cuál quieres cancelar?\n\n"
                for i, (pid, nc, desc, total, pagado, estado, mat_comp, fecha_mat, costo_mat, pres_comp, fecha_pres) in enumerate(proyectos, 1):
                    msg += f"{i}. *{nc or 'Proyecto'}* - {desc[:40]}...\n"
                msg += "\nResponde el número o el nombre."
                context.user_data['estado_espera'] = 'seleccion_cancelar'
                context.user_data['proyectos_cancelar'] = proyectos
                context.user_data['cliente_cancelar'] = cliente_nombre
                await update.message.reply_text(msg, parse_mode='Markdown')
                return

        # ===== BORRAR DEFINITIVAMENTE =====
        elif accion == "borrar_proyecto_definitivo":
            proyectos = obtener_proyectos_activos(cur, cliente_nombre)
            cur.close(); conn.close()
            if not proyectos:
                await update.message.reply_text(f"⚠️ No encontré proyectos activos para {cliente_nombre}, jefe.")
                return
            if len(proyectos) == 1:
                pid, nc, desc, total, pagado, estado, mat_comp, fecha_mat, costo_mat, pres_comp, fecha_pres = proyectos[0]
                context.user_data['estado_espera'] = 'confirmar_borrado'
                context.user_data['proyecto_borrar'] = pid
                context.user_data['cliente_borrar'] = cliente_nombre
                await update.message.reply_text(f"⚠️ **¿Seguro que quieres BORRAR DEFINITIVAMENTE '{nc or 'Proyecto'}' de {cliente_nombre}?**\nEsto no se deshace. Responde 'SÍ' para confirmar.", parse_mode='Markdown')
                return
            else:
                msg = f"👤 **{cliente_nombre}** tiene varios proyectos. ¿Cuál quieres borrar definitivamente? (o 'todos')\n\n"
                for i, (pid, nc, desc, total, pagado, estado, mat_comp, fecha_mat, costo_mat, pres_comp, fecha_pres) in enumerate(proyectos, 1):
                    msg += f"{i}. *{nc or 'Proyecto'}* - {desc[:40]}...\n"
                msg += "\nResponde el número, el nombre, o 'todos'."
                context.user_data['estado_espera'] = 'seleccion_borrado'
                context.user_data['proyectos_borrar'] = proyectos
                context.user_data['cliente_borrar'] = cliente_nombre
                await update.message.reply_text(msg, parse_mode='Markdown')
                return

        # ===== REGISTRAR PAGO =====
        elif accion == "registrar_pago":
            buscar_o_crear_cliente(cur, cliente_nombre, telefono, direccion)
            _, msg_pago = registrar_pago(cur, cliente_nombre, monto)
            respuesta = f"💰 **Pago registrado, patrón:**\n{resumen_ia}\n{msg_pago}"

        # ===== COMPRA DE MATERIAL =====
        elif accion == "registrar_compra_material":
            if not cliente_nombre or cliente_nombre == "Desconocido":
                await update.message.reply_text("⚠️ ¿Para qué cliente compraste el material, jefe?")
                return
            proyectos = obtener_proyectos_activos(cur, cliente_nombre)
            cur.close(); conn.close()
            if not proyectos:
                await update.message.reply_text(f"⚠️ No tengo proyectos activos para {cliente_nombre}, jefe.")
                return
            if len(proyectos) == 1:
                pid, nc, desc, total, pagado, estado, mat_comp, fecha_mat, costo_mat, pres_comp, fecha_pres = proyectos[0]
                costo = datos.get("monto", 0)
                if costo == 0:
                    context.user_data['estado_espera'] = 'esperando_costo_material'
                    context.user_data['proyecto_id'] = pid
                    context.user_data['cliente_nombre'] = cliente_nombre
                    await update.message.reply_text(f"🤔 ¿Quieres registrar el costo del material para '{nc}', jefe? Responde el monto (ej: 4500) o 'no' para saltarlo.")
                    return
                else:
                    marcar_material_comprado(cur, pid, costo)
                    conn.commit()
                    cur.close(); conn.close()
                    await update.message.reply_text(f"✅ Material marcado como comprado para *{cliente_nombre} - {nc}*, jefe.\n💰 Costo: ${costo:.2f}")
                    return
            else:
                msg = f"👤 **{cliente_nombre}** tiene {len(proyectos)} proyectos:\n\n"
                for i, (pid, nc, desc, total, pagado, estado, mat_comp, fecha_mat, costo_mat, pres_comp, fecha_pres) in enumerate(proyectos, 1):
                    msg += f"{i}. *{nc or 'Proyecto'}* - {desc[:40]}...\n"
                msg += "\n¿Para cuál proyecto o 'todos'?"
                context.user_data['estado_espera'] = 'seleccion_material'
                context.user_data['proyectos_seleccion'] = proyectos
                context.user_data['cliente_nombre'] = cliente_nombre
                context.user_data['costo_sugerido'] = datos.get("monto", 0)
                await update.message.reply_text(msg, parse_mode='Markdown')
                return

        else:
            respuesta = f"⚠️ No sé cómo procesar '{accion}', jefe. ¿Puedes repetir?"

        conn.commit()
        cur.close(); conn.close()
        respuesta += "\n\n_(Si algo no está bien, dímelo y lo corregimos)_"
        await update.message.reply_text(respuesta, parse_mode='Markdown')

    except Exception as e:
        logging.error(f"🔴 Error: {e}")
        await update.message.reply_text(f"❌ Error interno: {str(e)[:150]}. Lo siento, jefe.")

# ==================== MANEJO DE MENSAJES ====================
async def manejar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text:
        texto = update.message.text
        estado = context.user_data.get('estado_espera')
        if estado == 'seleccion_material':
            await procesar_seleccion_material(update, context, texto)
        elif estado == 'esperando_costo_material':
            await procesar_costo_material(update, context, texto)
        elif estado == 'seleccion_borrado':
            await procesar_seleccion_borrado(update, context, texto)
        elif estado == 'confirmar_borrado':
            await procesar_confirmacion_borrado(update, context, texto)
        elif estado == 'confirmar_borrado_todos':
            await procesar_confirmacion_borrado_todos(update, context, texto)
        elif estado == 'seleccion_cancelar':
            await procesar_seleccion_cancelar(update, context, texto)
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
            await update.message.reply_text(f"📝 *\"{texto}\"*", parse_mode='Markdown')
            await manejar_mensaje(update, context)
        except Exception as e:
            await update.message.reply_text(f"❌ Error de audio: {str(e)[:100]}. Disculpe, jefe.")

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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_mensaje))
    app.add_handler(MessageHandler(filters.VOICE, manejar_mensaje))
    print("🤖 Bot CORREGIDO: Presupuesto, Material, Cancelación específica, Borrado con 'todos' y confirmación en minúscula...")
    app.run_polling(drop_pending_updates=True)
