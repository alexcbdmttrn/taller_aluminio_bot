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

INSTRUCCIONES MUY IMPORTANTES:
1. **BORRADO GRANULAR NUNCA TODOS DE UNA VEZ**: Si el jefe dice "borrar" y no especifica si son activos, cancelados o liquidados, DEBES preguntar: "¿Qué tipo de proyectos quieres borrar? (activos, cancelados, liquidados, o gastos)". NUNCA asumas "todos".
2. **CONFIRMACIONES**: "si", "sí", "esta bien", "ok" → NO crees nuevo proyecto, solo confirma lo anterior.
3. **NUEVO PROYECTO**: "registra", "anota", "nuevo" + cliente + trabajo.
4. **PRESUPUESTO**: "ya mandé presupuesto" → accion "marcar_presupuesto_enviado". Si da monto, actualiza.
5. **MATERIAL**: "compré material", "ya compré" → accion "registrar_compra_material". Si da monto, lo guarda.
6. **CONSULTAS**: "qué clientes tengo" → accion "consultar". "presupuesto de Juan" → "consultar_presupuesto". "material de Juan" → "consultar_material".
7. **CANCELAR**: "cancela" + nombre específico → accion "cancelar_proyecto". NUNCA canceles todos sin preguntar.
8. **RESPUESTAS CON ICONOS**: Usa emojis útiles para orientar al jefe: 📋 para listas, ⚠️ para advertencias, 🗑️ para borrado, ✅ para éxito, ❌ para error, 🤔 para preguntas.

Responde SOLO con este JSON:
{{
  "accion": "registrar_proyecto" | "registrar_pago" | "actualizar_proyecto" | "marcar_presupuesto_enviado" | "cancelar_proyecto" | "borrar_proyecto_definitivo" | "consultar" | "consultar_historial" | "consultar_material" | "consultar_presupuesto" | "registrar_compra_material" | "borrar_por_tipo" | "preguntar",
  "cliente": "nombre",
  "nombre_corto": "nombre breve del proyecto",
  "monto": numero o 0,
  "descripcion": "detalles",
  "notas": "",
  "estado": "Pendiente de cotizar" | "Presupuesto enviado" | "Aceptado" | "En proceso" | "Por cobrar" | "Liquidado" | "Cancelado",
  "tipo_borrado": "activos" | "cancelados" | "liquidados" | "gastos" | null,
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

def obtener_proyectos_por_estado(cur, estado, cliente_nombre=None):
    if cliente_nombre:
        cur.execute("""
            SELECT p.id, c.nombre as cliente, p.nombre_corto, p.descripcion, p.monto_total, p.estado
            FROM proyectos p
            JOIN clientes c ON p.cliente_id = c.id
            WHERE p.estado = %s AND c.nombre ILIKE %s
            ORDER BY c.nombre, p.fecha_creacion DESC
        """, (estado, f"%{cliente_nombre}%"))
    else:
        cur.execute("""
            SELECT p.id, c.nombre as cliente, p.nombre_corto, p.descripcion, p.monto_total, p.estado
            FROM proyectos p
            JOIN clientes c ON p.cliente_id = c.id
            WHERE p.estado = %s
            ORDER BY c.nombre, p.fecha_creacion DESC
        """, (estado,))
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

def resolver_proyecto_activo(cur, cliente_nombre, referencia):
    referencia = (referencia or "").strip()
    if referencia and referencia.lower() not in ("proyecto general", "desconocido", ""):
        cur.execute("""
            SELECT p.id, p.nombre_corto FROM proyectos p
            JOIN clientes c ON p.cliente_id = c.id
            WHERE c.nombre ILIKE %s AND p.estado NOT IN ('Liquidado', 'Cancelado')
            AND (p.nombre_corto ILIKE %s OR p.descripcion ILIKE %s)
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
        WHERE c.nombre ILIKE %s AND p.estado NOT IN ('Liquidado', 'Cancelado')
        ORDER BY p.fecha_creacion DESC
    """, (f"%{cliente_nombre}%",))
    activos = cur.fetchall()
    if len(activos) == 1:
        return activos[0][0], []
    return None, activos

def actualizar_proyecto(cur, cliente_nombre, nombre_corto, descripcion, monto, estado, notas="", presupuesto_enviado=None):
    proyecto_id, candidatos = resolver_proyecto_activo(cur, cliente_nombre, nombre_corto or descripcion)
    if proyecto_id is None:
        return False, candidatos
    updates = ["nombre_corto = COALESCE(%s, nombre_corto)", "descripcion = COALESCE(%s, descripcion)",
               "monto_total = CASE WHEN %s > 0 THEN %s ELSE monto_total END",
               "estado = COALESCE(%s, estado)", "notas_adicionales = COALESCE(%s, notas_adicionales)"]
    params = [nombre_corto or None, descripcion or None, monto, monto, estado or None, notas or None]
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

def marcar_presupuesto_enviado(cur, cliente_nombre, nombre_corto):
    proyecto_id, candidatos = resolver_proyecto_activo(cur, cliente_nombre, nombre_corto)
    if proyecto_id is None:
        return False, candidatos
    cur.execute("""
        UPDATE proyectos 
        SET presupuesto_enviado = TRUE, fecha_presupuesto = CURRENT_TIMESTAMP
        WHERE id = %s
    """, (proyecto_id,))
    return True, []

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

def borrar_proyectos_por_estado(cur, estado, cliente_nombre=None):
    if cliente_nombre:
        cur.execute("""
            DELETE FROM proyectos 
            USING clientes c 
            WHERE proyectos.cliente_id = c.id 
            AND c.nombre ILIKE %s 
            AND proyectos.estado = %s
        """, (f"%{cliente_nombre}%", estado))
    else:
        cur.execute("DELETE FROM proyectos WHERE estado = %s", (estado,))
    return cur.rowcount

def borrar_gastos_por_tipo(cur, tipo_gasto, cliente_nombre=None):
    # tipo_gasto puede ser 'material' o 'general'
    if tipo_gasto == 'material':
        if cliente_nombre:
            cur.execute("DELETE FROM compras_material WHERE cliente_nombre ILIKE %s", (f"%{cliente_nombre}%",))
        else:
            cur.execute("DELETE FROM compras_material")
    elif tipo_gasto == 'general':
        if cliente_nombre:
            # Para gastos generales no hay cliente, así que si se pasa cliente_nombre, se ignora
            cur.execute("DELETE FROM gastos_generales")
        else:
            cur.execute("DELETE FROM gastos_generales")
    return cur.rowcount

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
        cur.execute("""
            SELECT c.nombre, p.nombre_corto, p.descripcion, p.monto_total, p.monto_pagado, p.estado,
                   p.presupuesto_enviado, p.material_comprado
            FROM proyectos p JOIN clientes c ON p.cliente_id = c.id 
            WHERE c.nombre ILIKE %s AND p.estado NOT IN ('Liquidado', 'Cancelado')
            ORDER BY p.fecha_creacion DESC
        """, (f"%{cliente_nombre}%",))
    else:
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
        "📌 Comandos principales:\n"
        "/cliente [nombre] - Forzar cliente activo\n"
        "/estadisticas - Resumen de proyectos\n"
        "/historial [nombre] - Historial de un cliente\n"
        "/resumen - Ver clientes activos\n"
        "/material [nombre] - Consultar material comprado\n"
        "/presupuesto [nombre] - Consultar presupuestos enviados\n"
        "🗑️ Para borrar, dígame 'borrar' y le guiaré."
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

async def procesar_seleccion_presupuesto(update, context, respuesta):
    try:
        candidatos = context.user_data.get('candidatos_presupuesto', [])
        cliente_nombre = context.user_data.get('cliente_presupuesto', '')
        if not candidatos:
            await update.message.reply_text("⚠️ No tengo proyectos en memoria, jefe.")
            context.user_data['estado_espera'] = None
            return
        seleccion = None
        resp_lower = respuesta.lower().strip()
        try:
            num = int(resp_lower)
            if 1 <= num <= len(candidatos):
                seleccion = num
        except ValueError:
            pass
        if seleccion is None:
            for i, (pid, nc) in enumerate(candidatos, 1):
                if nc and nc.lower() in resp_lower:
                    seleccion = i
                    break
        if seleccion is None:
            await update.message.reply_text("⚠️ No entendí, responde con el número o el nombre del proyecto.")
            return
        pid = candidatos[seleccion-1][0]
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE proyectos SET presupuesto_enviado = TRUE, fecha_presupuesto = CURRENT_TIMESTAMP WHERE id = %s", (pid,))
        conn.commit()
        cur.close(); conn.close()
        await update.message.reply_text(f"📋 Presupuesto marcado como ENVIADO para {cliente_nombre}, jefe.")
        context.user_data['estado_espera'] = None
        context.user_data['candidatos_presupuesto'] = None
        context.user_data['cliente_presupuesto'] = None
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
        seleccion = None
        resp_lower = respuesta.lower().strip()
        try:
            num = int(resp_lower)
            if 1 <= num <= len(candidatos):
                seleccion = num
        except ValueError:
            pass
        if seleccion is None:
            for i, (pid, nc) in enumerate(candidatos, 1):
                if nc and nc.lower() in resp_lower:
                    seleccion = i
                    break
        if seleccion is None:
            await update.message.reply_text("⚠️ No entendí, responde con el número o el nombre del proyecto.")
            return
        pid = candidatos[seleccion-1][0]
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

async def procesar_borrado_por_tipo(update, context, respuesta):
    try:
        tipo = context.user_data.get('borrar_tipo')  # 'activos', 'cancelados', 'liquidados', 'gastos'
        if not tipo:
            await update.message.reply_text("⚠️ No tengo el tipo de borrado en memoria, jefe.")
            context.user_data['estado_espera'] = None
            return
        
        # Mostrar lista de proyectos/gastos según el tipo
        conn = get_db_connection()
        cur = conn.cursor()
        
        if tipo == 'gastos':
            # Mostrar gastos generales y compras de material
            cur.execute("SELECT id, descripcion, monto, fecha FROM gastos_generales ORDER BY fecha DESC LIMIT 20")
            gastos = cur.fetchall()
            cur.close(); conn.close()
            if not gastos:
                await update.message.reply_text("📭 No hay gastos generales registrados, jefe.")
                context.user_data['estado_espera'] = None
                return
            msg = "🧾 **GASTOS GENERALES:**\n\n"
            for idx, (gid, desc, monto, fecha) in enumerate(gastos, 1):
                fecha_str = fecha.strftime("%d/%m %H:%M") if fecha else "Fecha desconocida"
                msg += f"{idx}. {fecha_str} | ${monto:.2f} - {desc}\n"
            msg += "\nEscribe los números separados por comas (ej: 1,3,5) o 'todos' para borrar."
            context.user_data['estado_espera'] = 'confirmar_borrado_gastos'
            context.user_data['gastos_a_borrar'] = gastos
            await update.message.reply_text(msg, parse_mode='Markdown')
            return
        
        # Para proyectos
        estado_map = {
            'activos': 'Activo',
            'cancelados': 'Cancelado',
            'liquidados': 'Liquidado'
        }
        if tipo not in estado_map:
            await update.message.reply_text("⚠️ Tipo de borrado no reconocido, jefe.")
            context.user_data['estado_espera'] = None
            return
        
        if tipo == 'activos':
            # Para activos, mostramos todos los que NO están Cancelados ni Liquidados
            cur.execute("""
                SELECT p.id, c.nombre, p.nombre_corto, p.descripcion, p.monto_total, p.estado
                FROM proyectos p
                JOIN clientes c ON p.cliente_id = c.id
                WHERE p.estado NOT IN ('Liquidado', 'Cancelado')
                ORDER BY c.nombre, p.fecha_creacion DESC
            """)
        else:
            cur.execute("""
                SELECT p.id, c.nombre, p.nombre_corto, p.descripcion, p.monto_total, p.estado
                FROM proyectos p
                JOIN clientes c ON p.cliente_id = c.id
                WHERE p.estado = %s
                ORDER BY c.nombre, p.fecha_creacion DESC
            """, (estado_map[tipo],))
        
        proyectos = cur.fetchall()
        cur.close(); conn.close()
        
        if not proyectos:
            await update.message.reply_text(f"📭 No hay proyectos {tipo}, jefe.")
            context.user_data['estado_espera'] = None
            return
        
        msg = f"📋 **PROYECTOS {tipo.upper()}:**\n\n"
        for idx, (pid, cliente, nc, desc, monto, estado) in enumerate(proyectos, 1):
            msg += f"{idx}. 👤 {cliente} | *{nc or 'Proyecto'}* - ${monto:.2f} ({estado})\n   {desc[:60]}...\n\n"
        
        msg += "Escribe los números separados por comas (ej: 1,3,5) o 'todos' para borrar."
        context.user_data['estado_espera'] = 'confirmar_borrado_proyectos'
        context.user_data['proyectos_a_borrar'] = proyectos
        context.user_data['borrar_tipo_estado'] = tipo
        await update.message.reply_text(msg, parse_mode='Markdown')
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
        context.user_data['estado_espera'] = None

async def procesar_confirmacion_borrado_proyectos(update, context, respuesta):
    try:
        proyectos = context.user_data.get('proyectos_a_borrar', [])
        if not proyectos:
            await update.message.reply_text("⚠️ No tengo proyectos en memoria, jefe.")
            context.user_data['estado_espera'] = None
            return
        
        resp_lower = respuesta.lower().strip()
        if resp_lower in ['todos', 'todo', 'todas']:
            ids = [p[0] for p in proyectos]
            tipo = context.user_data.get('borrar_tipo_estado', '')
            await update.message.reply_text(f"⚠️ **¿Seguro que quieres BORRAR DEFINITIVAMENTE TODOS los {len(ids)} proyectos {tipo}?**\nEsto no se deshace. Responde 'SÍ' para confirmar.", parse_mode='Markdown')
            context.user_data['estado_espera'] = 'confirmar_borrado_todos_estado'
            context.user_data['ids_borrar_todos'] = ids
            return
        
        # Parsear números separados por comas
        indices = []
        try:
            for part in resp_lower.split(','):
                part = part.strip()
                if part.isdigit():
                    idx = int(part)
                    if 1 <= idx <= len(proyectos):
                        indices.append(idx)
            if not indices:
                await update.message.reply_text("⚠️ No entendí. Escribe números separados por comas (ej: 1,3,5) o 'todos'.")
                return
        except:
            await update.message.reply_text("⚠️ Formato inválido. Escribe números separados por comas (ej: 1,3,5) o 'todos'.")
            return
        
        ids = [proyectos[idx-1][0] for idx in indices]
        await update.message.reply_text(f"⚠️ **¿Seguro que quieres BORRAR DEFINITIVAMENTE los {len(ids)} proyectos seleccionados?**\nEsto no se deshace. Responde 'SÍ' para confirmar.", parse_mode='Markdown')
        context.user_data['estado_espera'] = 'confirmar_borrado_ids'
        context.user_data['ids_borrar'] = ids
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
        context.user_data['estado_espera'] = None

async def procesar_confirmacion_borrado_ids(update, context, respuesta):
    try:
        ids = context.user_data.get('ids_borrar', [])
        if not ids:
            await update.message.reply_text("⚠️ No tengo IDs en memoria, jefe.")
            context.user_data['estado_espera'] = None
            return
        
        if respuesta.strip().upper() not in ['SÍ', 'SI']:
            await update.message.reply_text("✅ Borrado cancelado, jefe.")
            context.user_data['estado_espera'] = None
            return
        
        conn = get_db_connection()
        cur = conn.cursor()
        for pid in ids:
            borrar_proyecto_definitivo(cur, pid)
        conn.commit()
        cur.close(); conn.close()
        
        await update.message.reply_text(f"🗑️ **{len(ids)} proyectos borrados definitivamente, jefe.**", parse_mode='Markdown')
        context.user_data['estado_espera'] = None
        context.user_data['ids_borrar'] = None
        context.user_data['proyectos_a_borrar'] = None
        context.user_data['borrar_tipo_estado'] = None
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
        context.user_data['estado_espera'] = None

async def procesar_confirmacion_borrado_todos_estado(update, context, respuesta):
    try:
        ids = context.user_data.get('ids_borrar_todos', [])
        if not ids:
            await update.message.reply_text("⚠️ No tengo IDs en memoria, jefe.")
            context.user_data['estado_espera'] = None
            return
        
        if respuesta.strip().upper() not in ['SÍ', 'SI']:
            await update.message.reply_text("✅ Borrado cancelado, jefe.")
            context.user_data['estado_espera'] = None
            return
        
        conn = get_db_connection()
        cur = conn.cursor()
        for pid in ids:
            borrar_proyecto_definitivo(cur, pid)
        conn.commit()
        cur.close(); conn.close()
        
        await update.message.reply_text(f"🗑️ **{len(ids)} proyectos borrados definitivamente, jefe.**", parse_mode='Markdown')
        context.user_data['estado_espera'] = None
        context.user_data['ids_borrar_todos'] = None
        context.user_data['proyectos_a_borrar'] = None
        context.user_data['borrar_tipo_estado'] = None
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
        context.user_data['estado_espera'] = None

async def procesar_confirmacion_borrado_gastos(update, context, respuesta):
    try:
        gastos = context.user_data.get('gastos_a_borrar', [])
        if not gastos:
            await update.message.reply_text("⚠️ No tengo gastos en memoria, jefe.")
            context.user_data['estado_espera'] = None
            return
        
        resp_lower = respuesta.lower().strip()
        if resp_lower in ['todos', 'todo', 'todas']:
            ids = [g[0] for g in gastos]
            await update.message.reply_text(f"⚠️ **¿Seguro que quieres BORRAR DEFINITIVAMENTE TODOS los {len(ids)} gastos?**\nEsto no se deshace. Responde 'SÍ' para confirmar.", parse_mode='Markdown')
            context.user_data['estado_espera'] = 'confirmar_borrado_todos_gastos'
            context.user_data['ids_gastos_borrar_todos'] = ids
            return
        
        # Parsear números separados por comas
        indices = []
        try:
            for part in resp_lower.split(','):
                part = part.strip()
                if part.isdigit():
                    idx = int(part)
                    if 1 <= idx <= len(gastos):
                        indices.append(idx)
            if not indices:
                await update.message.reply_text("⚠️ No entendí. Escribe números separados por comas (ej: 1,3,5) o 'todos'.")
                return
        except:
            await update.message.reply_text("⚠️ Formato inválido. Escribe números separados por comas (ej: 1,3,5) o 'todos'.")
            return
        
        ids = [gastos[idx-1][0] for idx in indices]
        await update.message.reply_text(f"⚠️ **¿Seguro que quieres BORRAR DEFINITIVAMENTE los {len(ids)} gastos seleccionados?**\nEsto no se deshace. Responde 'SÍ' para confirmar.", parse_mode='Markdown')
        context.user_data['estado_espera'] = 'confirmar_borrado_ids_gastos'
        context.user_data['ids_gastos_borrar'] = ids
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
        context.user_data['estado_espera'] = None

async def procesar_confirmacion_borrado_ids_gastos(update, context, respuesta):
    try:
        ids = context.user_data.get('ids_gastos_borrar', [])
        if not ids:
            await update.message.reply_text("⚠️ No tengo IDs en memoria, jefe.")
            context.user_data['estado_espera'] = None
            return
        
        if respuesta.strip().upper() not in ['SÍ', 'SI']:
            await update.message.reply_text("✅ Borrado cancelado, jefe.")
            context.user_data['estado_espera'] = None
            return
        
        conn = get_db_connection()
        cur = conn.cursor()
        for gid in ids:
            cur.execute("DELETE FROM gastos_generales WHERE id = %s", (gid,))
        conn.commit()
        cur.close(); conn.close()
        
        await update.message.reply_text(f"🗑️ **{len(ids)} gastos borrados definitivamente, jefe.**", parse_mode='Markdown')
        context.user_data['estado_espera'] = None
        context.user_data['ids_gastos_borrar'] = None
        context.user_data['gastos_a_borrar'] = None
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
        context.user_data['estado_espera'] = None

async def procesar_confirmacion_borrado_todos_gastos(update, context, respuesta):
    try:
        ids = context.user_data.get('ids_gastos_borrar_todos', [])
        if not ids:
            await update.message.reply_text("⚠️ No tengo IDs en memoria, jefe.")
            context.user_data['estado_espera'] = None
            return
        
        if respuesta.strip().upper() not in ['SÍ', 'SI']:
            await update.message.reply_text("✅ Borrado cancelado, jefe.")
            context.user_data['estado_espera'] = None
            return
        
        conn = get_db_connection()
        cur = conn.cursor()
        for gid in ids:
            cur.execute("DELETE FROM gastos_generales WHERE id = %s", (gid,))
        conn.commit()
        cur.close(); conn.close()
        
        await update.message.reply_text(f"🗑️ **{len(ids)} gastos borrados definitivamente, jefe.**", parse_mode='Markdown')
        context.user_data['estado_espera'] = None
        context.user_data['ids_gastos_borrar_todos'] = None
        context.user_data['gastos_a_borrar'] = None
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
        context.user_data['estado_espera'] = None

# ==================== PROCESADOR PRINCIPAL ====================
async def ejecutar_una_accion(datos: dict, update: Update, context: ContextTypes.DEFAULT_TYPE, cliente_activo: str) -> bool:
    accion = datos.get("accion", "preguntar")

    # ===== PREGUNTAR =====
    if accion == "preguntar":
        pregunta = datos.get("pregunta", "¿Puedes darme más detalles, jefe?")
        historial = context.user_data.get('historial', [])
        historial.append(f"🤖 Bot: {pregunta}")
        context.user_data['historial'] = historial
        await update.message.reply_text(f"🤔 {pregunta}")
        return True

    # ===== BORRAR POR TIPO =====
    if accion == "borrar_por_tipo":
        tipo = datos.get("tipo_borrado")
        if not tipo:
            await update.message.reply_text("📋 ¿Qué tipo de proyectos quieres borrar? (activos, cancelados, liquidados, gastos)")
            return True
        if tipo not in ["activos", "cancelados", "liquidados", "gastos"]:
            await update.message.reply_text("⚠️ Opciones válidas: activos, cancelados, liquidados, gastos")
            return True
        
        context.user_data['borrar_tipo'] = tipo
        await procesar_borrado_por_tipo(update, context, "")
        return True

    # ===== CONSULTAR PRESUPUESTO =====
    if accion == "consultar_presupuesto":
        nombre_cliente = datos.get("cliente", cliente_activo if cliente_activo else "")
        if not nombre_cliente or nombre_cliente == "Desconocido":
            await update.message.reply_text("⚠️ ¿De qué cliente quieres saber el presupuesto, jefe?")
            return True
        context.args = [nombre_cliente]
        await comando_presupuesto(update, context)
        return False

    # ===== CONSULTAR MATERIAL =====
    if accion == "consultar_material":
        nombre_cliente = datos.get("cliente", cliente_activo if cliente_activo else "")
        if not nombre_cliente or nombre_cliente == "Desconocido":
            await update.message.reply_text("⚠️ ¿De qué cliente quieres saber el material, jefe?")
            return True
        context.args = [nombre_cliente]
        await comando_material(update, context)
        return False

    # ===== CONSULTAR HISTORIAL =====
    if accion == "consultar_historial":
        nombre_buscar = datos.get("cliente", cliente_activo)
        if nombre_buscar and nombre_buscar != "Desconocido":
            context.args = [nombre_buscar]
            await comando_historial(update, context)
        else:
            await update.message.reply_text("⚠️ ¿De qué cliente quieres el historial, jefe?")
        return False

    # ===== CONSULTAR (resumen general) =====
    if accion == "consultar":
        conn = get_db_connection()
        cur = conn.cursor()
        proyectos = consultar_proyectos(cur, "todos")
        cur.close(); conn.close()
        if not proyectos:
            await update.message.reply_text("📭 No hay clientes activos, jefe.")
            return False
        msg = "📊 **CLIENTES ACTIVOS:**\n\n"
        for n, nc, desc, t, p, e, pres_comp, mat_comp in proyectos:
            pendiente = t - p
            pres_icon = "📋" if pres_comp else "⏳"
            mat_icon = "🛠️" if mat_comp else "❌"
            msg += f"👤 *{n}*\n   Proyecto: {nc or 'Proyecto General'}\n   Detalle: {desc[:60]}...\n"
            msg += f"   Total: ${t:.2f} | Pagado: ${p:.2f} | Saldo: ${pendiente:.2f} | {e} | {pres_icon} Presupuesto | {mat_icon} Material\n\n"
        await update.message.reply_text(msg, parse_mode='Markdown')
        return False

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
        context.user_data['cliente_activo'] = cliente_nombre

    conn = get_db_connection()
    cur = conn.cursor()

    # ===== MARCAR PRESUPUESTO ENVIADO =====
    if accion == "marcar_presupuesto_enviado":
        marcado, candidatos = marcar_presupuesto_enviado(cur, cliente_nombre, nombre_corto)
        if not marcado and not candidatos:
            marcado, candidatos = marcar_presupuesto_enviado(cur, cliente_nombre, descripcion)
        if marcado:
            conn.commit()
            cur.close(); conn.close()
            respuesta = f"📋 **Presupuesto marcado como ENVIADO para {cliente_nombre}, jefe.**"
            respuesta += "\n\n_(Si algo no está bien, dímelo y lo corregimos)_"
            await update.message.reply_text(respuesta, parse_mode='Markdown')
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
        respuesta = f"✅ **Nuevo proyecto guardado, jefe:**\n{resumen_ia}\n Total: ${monto:.2f}"

    # ===== ACTUALIZAR PROYECTO =====
    elif accion == "actualizar_proyecto":
        buscar_o_crear_cliente(cur, cliente_nombre, telefono, direccion, notas)
        actualizado, candidatos = actualizar_proyecto(cur, cliente_nombre, nombre_corto, descripcion, monto, estado, notas)
        if actualizado:
            respuesta = f"✏️ **Proyecto actualizado, patrón:**\n{resumen_ia}"
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
            respuesta = f"⚠️ No encontré proyectos activos para {cliente_nombre}, jefe."

    # ===== CANCELAR PROYECTO =====
    elif accion == "cancelar_proyecto":
        proyectos = obtener_proyectos_activos(cur, cliente_nombre)
        cur.close(); conn.close()
        if not proyectos:
            await update.message.reply_text(f"⚠️ No encontré proyectos activos para {cliente_nombre}, jefe.")
            return False
        objetivo = (nombre_corto or descripcion or "").strip().lower()
        coincidencias = [p for p in proyectos if objetivo and objetivo not in ("proyecto general", "") and
                          ((p[1] and objetivo in p[1].lower()) or (p[2] and objetivo in p[2].lower()))]
        if len(proyectos) == 1:
            pid, nc = proyectos[0][0], proyectos[0][1]
            conn = get_db_connection(); cur = conn.cursor()
            cur.execute("UPDATE proyectos SET estado = 'Cancelado' WHERE id = %s", (pid,))
            conn.commit(); cur.close(); conn.close()
            await update.message.reply_text(f"🗑️ Proyecto '{nc or 'Proyecto'}' de {cliente_nombre} CANCELADO, jefe.")
            return False
        elif len(coincidencias) == 1:
            pid, nc = coincidencias[0][0], coincidencias[0][1]
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

    # ===== BORRAR DEFINITIVAMENTE (proyecto específico) =====
    elif accion == "borrar_proyecto_definitivo":
        proyectos = obtener_proyectos_activos(cur, cliente_nombre)
        cur.close(); conn.close()
        if not proyectos:
            await update.message.reply_text(f"⚠️ No encontré proyectos activos para {cliente_nombre}, jefe.")
            return False
        if len(proyectos) == 1:
            pid, nc, desc, total, pagado, estado, mat_comp, fecha_mat, costo_mat, pres_comp, fecha_pres = proyectos[0]
            context.user_data['estado_espera'] = 'confirmar_borrado'
            context.user_data['proyecto_borrar'] = pid
            context.user_data['cliente_borrar'] = cliente_nombre
            await update.message.reply_text(f"⚠️ **¿Seguro que quieres BORRAR DEFINITIVAMENTE '{nc or 'Proyecto'}' de {cliente_nombre}?**\nEsto no se deshace. Responde 'SÍ' para confirmar.", parse_mode='Markdown')
            return True
        else:
            msg = f"👤 **{cliente_nombre}** tiene varios proyectos. ¿Cuál quieres borrar definitivamente? (o 'todos')\n\n"
            for i, (pid, nc, desc, total, pagado, estado, mat_comp, fecha_mat, costo_mat, pres_comp, fecha_pres) in enumerate(proyectos, 1):
                msg += f"{i}. *{nc or 'Proyecto'}* - {desc[:40]}...\n"
            msg += "\nResponde el número, el nombre, o 'todos'."
            context.user_data['estado_espera'] = 'seleccion_borrado'
            context.user_data['proyectos_borrar'] = proyectos
            context.user_data['cliente_borrar'] = cliente_nombre
            await update.message.reply_text(msg, parse_mode='Markdown')
            return True

    # ===== REGISTRAR PAGO =====
    elif accion == "registrar_pago":
        buscar_o_crear_cliente(cur, cliente_nombre, telefono, direccion)
        _, msg_pago = registrar_pago(cur, cliente_nombre, monto)
        respuesta = f"💰 **Pago registrado, patrón:**\n{resumen_ia}\n{msg_pago}"

    # ===== COMPRA DE MATERIAL =====
    elif accion == "registrar_compra_material":
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
            pid, nc, desc, total, pagado, estado, mat_comp, fecha_mat, costo_mat, pres_comp, fecha_pres = proyectos[0]
            costo = datos.get("monto", 0) or 0
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
            context.user_data['costo_sugerido'] = datos.get("monto", 0) or 0
            await update.message.reply_text(msg, parse_mode='Markdown')
            return True

    else:
        respuesta = f"⚠️ No sé cómo procesar '{accion}', jefe. ¿Puedes repetir?"

    conn.commit()
    cur.close(); conn.close()
    respuesta += "\n\n_(Si algo no está bien, dímelo y lo corregimos)_"
    await update.message.reply_text(respuesta, parse_mode='Markdown')
    return False

async def procesar_texto(update: Update, context: ContextTypes.DEFAULT_TYPE, texto_original: str):
    try:
        historial = context.user_data.get('historial', [])
        cliente_activo = context.user_data.get('cliente_activo', '')

        historial.append(f"👤 Jefe: {texto_original}")
        if len(historial) > 15:
            historial = historial[-15:]
        context.user_data['historial'] = historial

        datos_completo = analizar_con_ia(texto_original, historial, cliente_activo, "")

        # Si la IA devuelve una lista "acciones", las procesamos
        acciones = datos_completo.get("acciones")
        if not isinstance(acciones, list) or not acciones:
            acciones = [datos_completo]

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
        elif estado == 'seleccion_presupuesto':
            await procesar_seleccion_presupuesto(update, context, texto)
        elif estado == 'seleccion_actualizar':
            await procesar_seleccion_actualizar(update, context, texto)
        elif estado == 'confirmar_borrado_proyectos':
            await procesar_confirmacion_borrado_proyectos(update, context, texto)
        elif estado == 'confirmar_borrado_ids':
            await procesar_confirmacion_borrado_ids(update, context, texto)
        elif estado == 'confirmar_borrado_todos_estado':
            await procesar_confirmacion_borrado_todos_estado(update, context, texto)
        elif estado == 'confirmar_borrado_gastos':
            await procesar_confirmacion_borrado_gastos(update, context, texto)
        elif estado == 'confirmar_borrado_ids_gastos':
            await procesar_confirmacion_borrado_ids_gastos(update, context, texto)
        elif estado == 'confirmar_borrado_todos_gastos':
            await procesar_confirmacion_borrado_todos_gastos(update, context, texto)
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
    print("🤖 Bot COMPLETO: Registro, Presupuesto, Material, Cancelación, Borrado granular (activos/cancelados/liquidados/gastos) y confirmación segura.")
    app.run_polling(drop_pending_updates=True)
