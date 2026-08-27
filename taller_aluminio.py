import os
import json
import logging
import psycopg2
from openai import OpenAI
from groq import Groq
from telegram import Update
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

# 2. INTELIGENCIA ARTIFICIAL CON MEMORIA Y CONFIRMACIÓN
def analizar_con_ia(texto, historial_mensajes, cliente_activo=""):
    contexto = ""
    if cliente_activo:
        contexto += f"[Cliente activo actual: {cliente_activo}]\n"
    
    if historial_mensajes:
        contexto += "[Historial de los últimos mensajes]:\n"
        for msg in historial_mensajes[-10:]:
            contexto += f"- {msg}\n"
    
    contexto += f"\n[Mensaje actual del jefe]: {texto}"
    
    prompt = f"""Eres el secretario experto de un taller de aluminio. Entiendes modismos y lenguaje coloquial.

Analiza el mensaje usando el CONTEXTO y el HISTORIAL. 
Responde SOLO con un objeto JSON válido con esta estructura exacta:
{{
  "accion": "registrar_proyecto" | "registrar_pago" | "registrar_compra" | "actualizar_proyecto" | "consultar" | "preguntar",
  "cliente": "nombre del cliente o el cliente_activo",
  "monto": numero o 0,
  "descripcion": "resumen breve del trabajo o gasto",
  "telefono": "número si lo menciona, o vacío",
  "direccion": "dirección si la menciona, o vacío",
  "estado": "Pendiente de cotizar" | "Aceptado" | "En proceso" | "Por cobrar" | "Liquidado",
  "tipo_consulta": "deudores" | "pendientes" | "todos" | "liquidados",
  "pregunta": "texto si necesitas preguntar algo",
  "resumen": "Frase corta y clara de lo que vas a guardar o actualizar"
}}

REGLAS DE ORO:
1. Si el jefe menciona un cliente y un trabajo que ya existe en el historial, o usa palabras como "cambia", "modifica", "no, era", "actualiza", la accion DEBE ser "actualizar_proyecto".
2. Si falta info crítica y no está en el historial, accion: "preguntar".
3. Si habla de COMPRAR, accion: "registrar_compra".
4. Si habla de COBRAR/PAGAR, accion: "registrar_pago".
5. Si habla de un NUEVO TRABAJO (no mencionado antes), accion: "registrar_proyecto".
6. Si pregunta quién debe o resumen, accion: "consultar".
7. El campo "resumen" es OBLIGATORIO.

{contexto}
Responde ÚNICAMENTE con el JSON."""

    response = deepseek_client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1
    )
    
    texto_respuesta = response.choices[0].message.content
    texto_limpio = texto_respuesta.replace("```json", "").replace("```", "").strip()
    return json.loads(texto_limpio)

# 3. AUDIO (GROQ)
def transcribir_audio(ruta_archivo):
    with open(ruta_archivo, "rb") as file:
        transcription = groq_client.audio.transcriptions.create(
            file=(ruta_archivo, file.read()),
            model="whisper-large-v3",
            language="es"
        )
    return transcription.text

# 4. BASE DE DATOS
def buscar_o_crear_cliente(cur, nombre_cliente, telefono="", direccion=""):
    cur.execute("SELECT id FROM clientes WHERE nombre ILIKE %s", (f"%{nombre_cliente}%",))
    cliente = cur.fetchone()
    
    if cliente:
        cliente_id = cliente[0]
        if telefono or direccion:
            cur.execute("""UPDATE clientes SET 
                           telefono = COALESCE(%s, telefono), 
                           direccion = COALESCE(%s, direccion) 
                           WHERE id = %s""", (telefono or None, direccion or None, cliente_id))
        return cliente_id
    else:
        cur.execute("INSERT INTO clientes (nombre, telefono, direccion) VALUES (%s, %s, %s) RETURNING id", 
                    (nombre_cliente, telefono or None, direccion or None))
        return cur.fetchone()[0]

def registrar_proyecto(cur, cliente_id, descripcion, monto, estado):
    cur.execute("""INSERT INTO proyectos (cliente_id, descripcion, monto_total, monto_pagado, estado) 
                   VALUES (%s, %s, %s, 0, %s) RETURNING id""", 
                (cliente_id, descripcion, monto, estado))
    return cur.fetchone()[0]

def actualizar_proyecto(cur, cliente_nombre, descripcion, monto, estado):
    cur.execute("""
        SELECT p.id FROM proyectos p
        JOIN clientes c ON p.cliente_id = c.id
        WHERE c.nombre ILIKE %s AND p.estado != 'Liquidado'
        ORDER BY p.fecha_creacion DESC LIMIT 1
    """, (f"%{cliente_nombre}%",))
    proyecto = cur.fetchone()
    
    if proyecto:
        cur.execute("""
            UPDATE proyectos 
            SET descripcion = %s, monto_total = %s, estado = %s
            WHERE id = %s
        """, (descripcion, monto, estado, proyecto[0]))
        return True
    return False

def registrar_pago(cur, cliente_nombre, monto_pago):
    cur.execute("""SELECT p.id, p.monto_total, p.monto_pagado, p.estado
                   FROM proyectos p JOIN clientes c ON p.cliente_id = c.id
                   WHERE c.nombre ILIKE %s AND p.estado != 'Liquidado'
                   ORDER BY p.fecha_creacion DESC LIMIT 1""", (f"%{cliente_nombre}%",))
    proyecto = cur.fetchone()
    
    if not proyecto:
        return None, "No encontré proyectos pendientes para este cliente."
    
    proyecto_id, monto_total, monto_pagado, estado_actual = proyecto
    nuevo_pagado = monto_pagado + monto_pago
    saldo = max(0, monto_total - nuevo_pagado)
    nuevo_estado = "Liquidado" if saldo == 0 else "Por cobrar"
    if monto_pago > 0 and estado_actual == "Pendiente de cotizar":
        nuevo_estado = "En proceso"
        
    cur.execute("UPDATE proyectos SET monto_pagado = %s, estado = %s WHERE id = %s", 
                (nuevo_pagado, nuevo_estado, proyecto_id))
    return proyecto_id, f"Saldo restante: ${saldo:.2f}. Estado: {nuevo_estado}"

def registrar_compra(cur, descripcion, monto):
    cur.execute("INSERT INTO proyectos (cliente_id, descripcion, monto_total, monto_pagado, estado) VALUES (0, %s, %s, %s, 'Gasto/Material')", 
                (descripcion, monto, monto))
    return cur.rowcount > 0

def consultar_proyectos(cur, tipo):
    if tipo == "deudores":
        cur.execute("""SELECT c.nombre, p.descripcion, p.monto_total, p.monto_pagado, (p.monto_total - p.monto_pagado) as saldo
                       FROM proyectos p JOIN clientes c ON p.cliente_id = c.id
                       WHERE p.estado IN ('Por cobrar', 'En proceso') AND p.monto_total > p.monto_pagado ORDER BY saldo DESC""")
    elif tipo == "pendientes":
        cur.execute("""SELECT c.nombre, p.descripcion, p.monto_total FROM proyectos p JOIN clientes c ON p.cliente_id = c.id WHERE p.estado = 'Pendiente de cotizar'""")
    elif tipo == "liquidados":
        cur.execute("""SELECT c.nombre, p.descripcion, p.monto_total FROM proyectos p JOIN clientes c ON p.cliente_id = c.id WHERE p.estado = 'Liquidado' LIMIT 10""")
    else:
        cur.execute("""SELECT c.nombre, p.descripcion, p.monto_total, p.monto_pagado, p.estado FROM proyectos p JOIN clientes c ON p.cliente_id = c.id ORDER BY p.fecha_creacion DESC LIMIT 15""")
    return cur.fetchall()

# 5. MANEJO DE MENSAJES
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['historial'] = []
    context.user_data['cliente_activo'] = ""
    await update.message.reply_text(
        "¡Hola Jefe! ️ Asistente listo.\n\n"
        "Háblame natural. Si corriges algo, lo actualizaré en lugar de duplicarlo."
    )

async def comando_cliente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        cliente_nombre = " ".join(context.args)
        context.user_data['cliente_activo'] = cliente_nombre
        await update.message.reply_text(f"✅ Cliente activo: **{cliente_nombre}**", parse_mode='Markdown')
    else:
        actual = context.user_data.get('cliente_activo', 'Ninguno')
        await update.message.reply_text(f" Cliente activo: **{actual}**", parse_mode='Markdown')

async def procesar_texto(update: Update, context: ContextTypes.DEFAULT_TYPE, texto_original: str):
    try:
        historial = context.user_data.get('historial', [])
        cliente_activo = context.user_data.get('cliente_activo', '')
        
        historial.append(texto_original)
        if len(historial) > 10:
            historial = historial[-10:]
        
        datos = analizar_con_ia(texto_original, historial, cliente_activo)
        accion = datos.get("accion", "preguntar")
        
        historial.append(f"[Sistema: La IA entendió {accion}]")
        context.user_data['historial'] = historial
        
        if accion == "preguntar":
            pregunta = datos.get("pregunta", "¿Me das más detalles?")
            await update.message.reply_text(f"🤔 {pregunta}")
            return

        if accion == "consultar":
            tipo = datos.get("tipo_consulta", "todos")
            conn = get_db_connection()
            cur = conn.cursor()
            proyectos = consultar_proyectos(cur, tipo)
            cur.close(); conn.close()
            
            if not proyectos:
                await update.message.reply_text("📭 No hay nada aquí.")
                return
                
            msg = ""
            if tipo == "deudores":
                msg = "🔴 **DEUDORES:**\n\n"
                for n, d, t, p, s in proyectos: msg += f"👤 {n}\n🔧 {d}\n💰 **Debe: ${s:.2f}**\n\n"
            elif tipo == "pendientes":
                msg = "📝 **POR COTIZAR:**\n\n"
                for n, d, t in proyectos: msg += f"👤 {n} - {d} (${t:.2f})\n"
            elif tipo == "liquidados":
                msg = "✅ **LIQUIDADOS:**\n\n"
                for n, d, t in proyectos: msg += f"👤 {n} - {d} (${t:.2f})\n"
            else:
                msg = " **RESUMEN:**\n\n"
                for n, d, t, p, e in proyectos: msg += f"👤 {n} | {d} | ${t} (Pagado: ${p}) | {e}\n\n"
            await update.message.reply_text(msg, parse_mode='Markdown')
            return

        # Procesar datos
        cliente_nombre = datos.get("cliente", cliente_activo if cliente_activo else "Desconocido")
        monto = float(datos.get("monto", 0))
        descripcion = datos.get("descripcion", texto_original)
        estado = datos.get("estado", "Pendiente de cotizar")
        telefono = datos.get("telefono", "")
        direccion = datos.get("direccion", "")
        resumen_ia = datos.get("resumen", "")

        if cliente_nombre != "Desconocido" and cliente_nombre != cliente_activo:
            context.user_data['cliente_activo'] = cliente_nombre

        conn = get_db_connection()
        cur = conn.cursor()

        if accion == "registrar_proyecto":
            buscar_o_crear_cliente(cur, cliente_nombre, telefono, direccion)
            cliente_id = buscar_o_crear_cliente(cur, cliente_nombre)
            registrar_proyecto(cur, cliente_id, descripcion, monto, estado)
            respuesta = f"✅ **Nuevo proyecto guardado:**\n{resumen_ia}"
            
        elif accion == "actualizar_proyecto":
            buscar_o_crear_cliente(cur, cliente_nombre, telefono, direccion)
            actualizado = actualizar_proyecto(cur, cliente_nombre, descripcion, monto, estado)
            if actualizado:
                respuesta = f"️ **Proyecto actualizado:**\n{resumen_ia}"
            else:
                respuesta = f"⚠️ No encontré proyectos activos para {cliente_nombre} para actualizar."
            
        elif accion == "registrar_pago":
            buscar_o_crear_cliente(cur, cliente_nombre, telefono, direccion)
            _, msg_pago = registrar_pago(cur, cliente_nombre, monto)
            respuesta = f" **Pago registrado:**\n{resumen_ia}\n{msg_pago}"
            
        elif accion == "registrar_compra":
            registrar_compra(cur, descripcion, monto)
            respuesta = f"🛒 **Compra registrada:**\n{resumen_ia}"
            
        conn.commit()
        cur.close(); conn.close()
        
        respuesta += "\n\n_(Si está mal, dime qué cambiar)_"
        await update.message.reply_text(respuesta, parse_mode='Markdown')

    except Exception as e:
        logging.error(f"🔴 Error: {e}")
        await update.message.reply_text(f" Error: {str(e)[:150]}")

async def manejar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text:
        await procesar_texto(update, context, update.message.text)
    elif update.message.voice:
        try:
            await update.message.reply_text("️ Escuchando...")
            file = await context.bot.get_file(update.message.voice.file_id)
            ruta = 'voice.ogg'
            await file.download_to_drive(ruta)
            texto = transcribir_audio(ruta)
            os.remove(ruta)
            await update.message.reply_text(f"📝 *\"{texto}\"*", parse_mode='Markdown')
            await procesar_texto(update, context, texto)
        except Exception as e:
            await update.message.reply_text(f"❌ Error de audio: {str(e)[:100]}")

# 6. INICIO
if __name__ == '__main__':
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cliente", comando_cliente))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_mensaje))
    app.add_handler(MessageHandler(filters.VOICE, manejar_mensaje))
    print("🤖 Bot final con memoria, confirmación y actualizaciones inteligentes iniciado...")
    app.run_polling(drop_pending_updates=True)
