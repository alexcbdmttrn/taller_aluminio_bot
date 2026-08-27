import os
import json
import logging
import psycopg2
from datetime import datetime
from openai import OpenAI
from groq import Groq
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# 1. CONFIGURACIÓN
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TOKEN = os.environ.get("TELEGRAM_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

deepseek_client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
groq_client = Groq(api_key=GROQ_API_KEY)

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

# 2. FUNCIÓN DE INTELIGENCIA ARTIFICIAL (DEEPSEEK)
def analizar_con_ia(texto):
    prompt = f"""Eres el secretario inteligente de un taller de aluminio. Analiza el mensaje del jefe y determina QUÉ QUIERE HACER.

Responde SOLO con un objeto JSON válido con esta estructura:
{{
  "accion": "registrar_proyecto" | "registrar_pago" | "consultar" | "actualizar_proyecto",
  "cliente": "nombre del cliente o Desconocido",
  "monto": numero o 0,
  "descripcion": "resumen breve",
  "estado": "Pendiente de cotizar" | "Aceptado" | "En proceso" | "Por cobrar" | "Liquidado",
  "tipo_consulta": "deudores" | "pendientes" | "todos" | "liquidados" (solo si accion es "consultar")
}}

REGLAS IMPORTANTES:
- Si el mensaje habla de COBRAR, PAGAR, ANTICIPO, o LIQUIDAR → accion: "registrar_pago"
- Si el mensaje habla de un NUEVO TRABAJO, PRESUPUESTO, o COTIZACIÓN → accion: "registrar_proyecto"
- Si pregunta QUIÉN DEBE, QUÉ HAY PENDIENTE, o RESUMEN → accion: "consultar"
- Si menciona un cliente existente y cambia algo (estado, monto) → accion: "actualizar_proyecto"
- El "estado" debe reflejar la situación REAL después de la acción

Mensaje del jefe: "{texto}"

Responde ÚNICAMENTE con el JSON, sin texto extra."""

    response = deepseek_client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1
    )
    
    texto_respuesta = response.choices[0].message.content
    texto_limpio = texto_respuesta.replace("```json", "").replace("```", "").strip()
    return json.loads(texto_limpio)

# 3. FUNCIÓN DE AUDIO (GROQ WHISPER)
def transcribir_audio(ruta_archivo):
    with open(ruta_archivo, "rb") as file:
        transcription = groq_client.audio.transcriptions.create(
            file=(ruta_archivo, file.read()),
            model="whisper-large-v3",
            language="es"
        )
    return transcription.text

# 4. FUNCIONES DE BASE DE DATOS
def buscar_o_crear_cliente(cur, nombre_cliente):
    cur.execute("SELECT id FROM clientes WHERE nombre ILIKE %s", (f"%{nombre_cliente}%",))
    cliente = cur.fetchone()
    
    if cliente:
        return cliente[0]
    else:
        cur.execute("INSERT INTO clientes (nombre) VALUES (%s) RETURNING id", (nombre_cliente,))
        return cur.fetchone()[0]

def registrar_proyecto(cur, cliente_id, descripcion, monto, estado):
    cur.execute("""
        INSERT INTO proyectos (cliente_id, descripcion, monto_total, monto_pagado, estado) 
        VALUES (%s, %s, %s, 0, %s) RETURNING id
    """, (cliente_id, descripcion, monto, estado))
    return cur.fetchone()[0]

def registrar_pago(cur, cliente_nombre, monto_pago, descripcion_pago):
    # Buscar el proyecto más reciente del cliente que no esté liquidado
    cur.execute("""
        SELECT p.id, p.monto_total, p.monto_pagado, p.estado
        FROM proyectos p
        JOIN clientes c ON p.cliente_id = c.id
        WHERE c.nombre ILIKE %s AND p.estado != 'Liquidado'
        ORDER BY p.fecha_creacion DESC
        LIMIT 1
    """, (f"%{cliente_nombre}%",))
    
    proyecto = cur.fetchone()
    
    if not proyecto:
        return None, "No encontré proyectos pendientes para este cliente."
    
    proyecto_id, monto_total, monto_pagado, estado_actual = proyecto
    nuevo_monto_pagado = monto_pagado + monto_pago
    saldo_faltante = monto_total - nuevo_monto_pagado
    
    # Determinar nuevo estado
    if saldo_faltante <= 0:
        nuevo_estado = "Liquidado"
    elif monto_pago > 0 and estado_actual == "Pendiente de cotizar":
        nuevo_estado = "Aceptado"
    elif estado_actual in ["Aceptado", "En proceso"]:
        nuevo_estado = "Por cobrar"
    else:
        nuevo_estado = estado_actual
    
    cur.execute("""
        UPDATE proyectos 
        SET monto_pagado = %s, estado = %s 
        WHERE id = %s
    """, (nuevo_monto_pagado, nuevo_estado, proyecto_id))
    
    return proyecto_id, f"Pago registrado. Nuevo saldo: ${saldo_faltante:.2f}. Estado: {nuevo_estado}"

def consultar_proyectos(cur, tipo_consulta):
    if tipo_consulta == "deudores":
        cur.execute("""
            SELECT c.nombre, p.descripcion, p.monto_total, p.monto_pagado, 
                   (p.monto_total - p.monto_pagado) as saldo
            FROM proyectos p
            JOIN clientes c ON p.cliente_id = c.id
            WHERE p.estado IN ('Por cobrar', 'En proceso') AND p.monto_total > p.monto_pagado
            ORDER BY saldo DESC
        """)
    elif tipo_consulta == "pendientes":
        cur.execute("""
            SELECT c.nombre, p.descripcion, p.monto_total, p.estado
            FROM proyectos p
            JOIN clientes c ON p.cliente_id = c.id
            WHERE p.estado = 'Pendiente de cotizar'
            ORDER BY p.fecha_creacion DESC
        """)
    elif tipo_consulta == "liquidados":
        cur.execute("""
            SELECT c.nombre, p.descripcion, p.monto_total, p.fecha_creacion
            FROM proyectos p
            JOIN clientes c ON p.cliente_id = c.id
            WHERE p.estado = 'Liquidado'
            ORDER BY p.fecha_creacion DESC
            LIMIT 10
        """)
    else:  # todos
        cur.execute("""
            SELECT c.nombre, p.descripcion, p.monto_total, p.monto_pagado, p.estado
            FROM proyectos p
            JOIN clientes c ON p.cliente_id = c.id
            ORDER BY p.fecha_creacion DESC
            LIMIT 20
        """)
    
    return cur.fetchall()

# 5. COMANDOS Y MENSAJES
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "¡Hola! Soy el asistente del taller. 🛠️\n\n"
        "Puedo ayudarte con:\n"
        "• Registrar nuevos proyectos y cobros\n"
        "• Consultar deudores y pendientes\n"
        "• Transcribir notas de voz\n\n"
        "Comandos rápidos:\n"
        "/resumen - Ver todos los proyectos\n"
        "/deudores - Ver quién debe dinero\n"
        "/pendientes - Ver presupuestos sin cotizar\n"
        "/liquidados - Ver proyectos cerrados\n\n"
        "O simplemente háblame natural:\n"
        "'La Sra. Elena me dio 2000 de anticipo'"
    )

async def comando_resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await consultar_y_responder(update, "todos")

async def comando_deudores(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await consultar_y_responder(update, "deudores")

async def comando_pendientes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await consultar_y_responder(update, "pendientes")

async def comando_liquidados(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await consultar_y_responder(update, "liquidados")

async def consultar_y_responder(update: Update, tipo_consulta):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        proyectos = consultar_proyectos(cur, tipo_consulta)
        cur.close()
        conn.close()
        
        if not proyectos:
            await update.message.reply_text(" No hay proyectos en esta categoría.")
            return
        
        mensaje = ""
        if tipo_consulta == "deudores":
            mensaje = "🔴 **DEUDORES** (quienes deben dinero):\n\n"
            for nombre, desc, total, pagado, saldo in proyectos:
                mensaje += f" {nombre}\n🔧 {desc}\n💰 Total: ${total:.2f} | Pagado: ${pagado:.2f}\n📌 **Debe: ${saldo:.2f}**\n\n"
        elif tipo_consulta == "pendientes":
            mensaje = "📝 **PRESUPUESTOS PENDIENTES**:\n\n"
            for nombre, desc, total, estado in proyectos:
                mensaje += f"👤 {nombre}\n🔧 {desc}\n💰 Monto: ${total:.2f}\n\n"
        elif tipo_consulta == "liquidados":
            mensaje = "✅ **PROYECTOS LIQUIDADOS** (últimos 10):\n\n"
            for nombre, desc, total, fecha in proyectos:
                mensaje += f"👤 {nombre} - {desc} (${total:.2f})\n"
        else:
            mensaje = "📊 **RESUMEN DE PROYECTOS**:\n\n"
            for nombre, desc, total, pagado, estado in proyectos:
                saldo = total - pagado
                mensaje += f"👤 {nombre}\n🔧 {desc}\n💰 ${total:.2f} | Pagado: ${pagado:.2f} | Saldo: ${saldo:.2f}\n📊 {estado}\n\n"
        
        await update.message.reply_text(mensaje, parse_mode='Markdown')
        
    except Exception as e:
        logging.error(f"Error en consulta: {e}")
        await update.message.reply_text(f" Error al consultar: {str(e)}")

async def procesar_texto(update: Update, texto_original: str):
    try:
        await update.message.reply_text("🧠 Procesando...")
        datos = analizar_con_ia(texto_original)
        
        accion = datos.get("accion", "registrar_proyecto")
        cliente_nombre = datos.get("cliente", "Desconocido")
        monto = float(datos.get("monto", 0))
        descripcion = datos.get("descripcion", texto_original)
        estado = datos.get("estado", "Pendiente de cotizar")
        tipo_consulta = datos.get("tipo_consulta", "todos")
        
        conn = get_db_connection()
        cur = conn.cursor()
        
        if accion == "registrar_proyecto":
            cliente_id = buscar_o_crear_cliente(cur, cliente_nombre)
            proyecto_id = registrar_proyecto(cur, cliente_id, descripcion, monto, estado)
            conn.commit()
            
            respuesta = (
                f"✅ **Proyecto registrado**\n\n"
                f"👤 Cliente: {cliente_nombre}\n"
                f"🔧 Trabajo: {descripcion}\n"
                f"💰 Monto: ${monto:.2f}\n"
                f"📊 Estado: {estado}"
            )
            
        elif accion == "registrar_pago":
            proyecto_id, mensaje_pago = registrar_pago(cur, cliente_nombre, monto, descripcion)
            conn.commit()
            
            if proyecto_id is None:
                respuesta = f"⚠️ {mensaje_pago}"
            else:
                respuesta = (
                    f"💰 **Pago registrado**\n\n"
                    f"👤 Cliente: {cliente_nombre}\n"
                    f"💵 Monto pagado: ${monto:.2f}\n"
                    f" {mensaje_pago}"
                )
            
        elif accion == "consultar":
            cur.close()
            conn.close()
            await consultar_y_responder(update, tipo_consulta)
            return
            
        elif accion == "actualizar_proyecto":
            # Buscar proyecto existente y actualizar
            cur.execute("""
                SELECT p.id FROM proyectos p
                JOIN clientes c ON p.cliente_id = c.id
                WHERE c.nombre ILIKE %s AND p.estado != 'Liquidado'
                ORDER BY p.fecha_creacion DESC
                LIMIT 1
            """, (f"%{cliente_nombre}%",))
            
            proyecto = cur.fetchone()
            if proyecto:
                cur.execute("""
                    UPDATE proyectos 
                    SET descripcion = %s, monto_total = %s, estado = %s
                    WHERE id = %s
                """, (descripcion, monto, estado, proyecto[0]))
                conn.commit()
                respuesta = (
                    f"✏️ **Proyecto actualizado**\n\n"
                    f"👤 Cliente: {cliente_nombre}\n"
                    f"🔧 Nuevo trabajo: {descripcion}\n"
                    f" Nuevo monto: ${monto:.2f}\n"
                    f"📊 Nuevo estado: {estado}"
                )
            else:
                respuesta = f"⚠️ No encontré proyectos activos para {cliente_nombre}."
        
        cur.close()
        conn.close()
        
        await update.message.reply_text(respuesta, parse_mode='Markdown')
        
    except Exception as e:
        logging.error(f"🔴 Error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def manejar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text:
        await procesar_texto(update, update.message.text)
        
    elif update.message.voice:
        try:
            await update.message.reply_text("🎙️ Escuchando y transcribiendo...")
            file = await context.bot.get_file(update.message.voice.file_id)
            ruta_audio = 'voice.ogg'
            await file.download_to_drive(ruta_audio)
            
            texto_transcrito = transcribir_audio(ruta_audio)
            os.remove(ruta_audio)
            
            await update.message.reply_text(f"📝 *Transcripción:* \"{texto_transcrito}\"", parse_mode='Markdown')
            await procesar_texto(update, texto_transcrito)
            
        except Exception as e:
            logging.error(f"🔴 Error de audio: {e}")
            await update.message.reply_text(f"❌ Error con audio: {str(e)}")

# 6. INICIO DEL BOT
if __name__ == '__main__':
    app = ApplicationBuilder().token(TOKEN).build()
    
    # Comandos
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("resumen", comando_resumen))
    app.add_handler(CommandHandler("deudores", comando_deudores))
    app.add_handler(CommandHandler("pendientes", comando_pendientes))
    app.add_handler(CommandHandler("liquidados", comando_liquidados))
    
    # Mensajes de texto y voz
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_mensaje))
    app.add_handler(MessageHandler(filters.VOICE, manejar_mensaje))
    
    print("🤖 Bot completo iniciado: DeepSeek + Groq + DB + 5 etapas + Consultas")
    app.run_polling(drop_pending_updates=True)
