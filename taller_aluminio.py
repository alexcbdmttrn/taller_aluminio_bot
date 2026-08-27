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

# 2. INTELIGENCIA ARTIFICIAL
def analizar_con_ia(texto, historial_mensajes, cliente_activo="", proyectos_existentes=""):
    contexto = ""
    if cliente_activo:
        contexto += f"[Cliente activo actual: {cliente_activo}]\n"
    
    if proyectos_existentes:
        contexto += f"[PROYECTOS EXISTENTES EN BASE DE DATOS]:\n{proyectos_existentes}\n"
    
    if historial_mensajes:
        contexto += "[Historial de los últimos mensajes]:\n"
        for msg in historial_mensajes[-10:]:
            contexto += f"- {msg}\n"
    
    contexto += f"\n[Mensaje actual del jefe]: {texto}"
    
    prompt = f"""Eres el secretario experto de un taller de aluminio.

Analiza el mensaje. 
Responde SOLO con un objeto JSON válido con esta estructura exacta:
{{
  "accion": "registrar_proyecto" | "registrar_pago" | "registrar_compra" | "actualizar_proyecto" | "cancelar_proyecto" | "consultar" | "preguntar" | "ignorar_duplicado",
  "cliente": "nombre del cliente",
  "nombre_corto": "Nombre breve del trabajo (ej: 'Cancel Baño'). OBLIGATORIO para proyectos.",
  "monto": numero o 0,
  "descripcion": "detalles técnicos",
  "notas": "notas adicionales o vacío",
  "telefono": "número o vacío",
  "direccion": "dirección o vacío",
  "estado": "Pendiente de cotizar" | "Aceptado" | "En proceso" | "Por cobrar" | "Liquidado" | "Cancelado",
  "tipo_consulta": "deudores" | "pendientes" | "todos" | "liquidados",
  "pregunta": "texto si necesitas preguntar algo",
  "resumen": "Frase corta de lo que harás"
}}

REGLAS CRÍTICAS:
1. CANCELAR/BORRAR: Si el jefe dice "borra", "cancela", "elimina", "descarta", "ya no quiere", "no le interesó", la accion DEBE ser "cancelar_proyecto".
2. ANTI-DUPLICADOS: Si en [PROYECTOS EXISTENTES] hay un proyecto muy similar al que pide, y NO está cancelando, accion: "ignorar_duplicado".
3. Si el jefe dice "cambia", "no era", "modifica", accion: "actualizar_proyecto".
4. Si falta info crítica, accion: "preguntar".
5. Si habla de COMPRAR, accion: "registrar_compra".
6. Si habla de COBRAR/PAGAR/ANTICIPO, accion: "registrar_pago".
7. Si es un trabajo NUEVO, accion: "registrar_proyecto".
8. Si pregunta quién debe o resumen, accion: "consultar".

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
    # Solo traemos los que NO están liquidados ni cancelados
    cur.execute("""
        SELECT p.id, p.nombre_corto, p.descripcion, p.monto_total, p.monto_pagado, p.estado 
        FROM proyectos p 
        JOIN clientes c ON p.cliente_id = c.id 
        WHERE c.nombre ILIKE %s AND p.estado NOT IN ('Liquidado', 'Cancelado')
        ORDER BY p.fecha_creacion DESC
    """, (f"%{cliente_nombre}%",))
    return cur.fetchall()

def registrar_proyecto(cur, cliente_id, nombre_corto, descripcion, monto, estado, notas=""):
    cur.execute("""INSERT INTO proyectos (cliente_id, nombre_corto, descripcion, monto_total, monto_pagado, estado, notas_adicionales) 
                   VALUES (%s, %s, %s, %s, 0, %s, %s) RETURNING id""", 
                (cliente_id, nombre_corto, descripcion, monto, estado, notas or None))
    return cur.fetchone()[0]

def actualizar_proyecto(cur, cliente_nombre, nombre_corto, descripcion, monto, estado, notas=""):
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
            SET nombre_corto = %s, descripcion = %s, monto_total = %s, estado = %s, notas_adicionales = COALESCE(%s, notas_adicionales)
            WHERE id = %s
        """, (nombre_corto, descripcion, monto, estado, notas or None, proyecto[0]))
        return True
    return False

def cancelar_proyecto(cur, cliente_nombre, nombre_corto):
    cur.execute("""
        SELECT p.id FROM proyectos p
        JOIN clientes c ON p.cliente_id = c.id
        WHERE c.nombre ILIKE %s AND (p.nombre_corto ILIKE %s OR p.estado NOT IN ('Liquidado', 'Cancelado'))
        ORDER BY p.fecha_creacion DESC LIMIT 1
    """, (f"%{cliente_nombre}%", f"%{nombre_corto}%"))
    proyecto = cur.fetchone()
    
    if proyecto:
        cur.execute("UPDATE proyectos SET estado = 'Cancelado' WHERE id = %s", (proyecto[0],))
        return True
    return False

def registrar_pago(cur, cliente_nombre, monto_pago):
    cur.execute("""SELECT p.id, p.monto_total, p.monto_pagado, p.estado
                   FROM proyectos p JOIN clientes c ON p.cliente_id = c.id
                   WHERE c.nombre ILIKE %s AND p.estado NOT IN ('Liquidado', 'Cancelado')
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
    return proyecto_id, f"Anticipo/Pago: ${monto_pago:.2f}. Saldo pendiente: ${saldo:.2f}. Estado: {nuevo_estado}"

def registrar_compra(cur, descripcion, monto):
    cur.execute("INSERT INTO proyectos (cliente_id, nombre_corto, descripcion, monto_total, monto_pagado, estado) VALUES (0, 'Compra Material', %s, %s, %s, 'Gasto/Material')", 
                (descripcion, monto, monto))
    return cur.rowcount > 0

def consultar_proyectos(cur, tipo):
    # Filtramos para que NUNCA muestre los Cancelados en las consultas normales
    if tipo == "deudores":
        cur.execute("""SELECT c.nombre, p.nombre_corto, p.monto_total, p.monto_pagado, (p.monto_total - p.monto_pagado) as saldo
                       FROM proyectos p JOIN clientes c ON p.cliente_id = c.id
                       WHERE p.estado IN ('Por cobrar', 'En proceso') AND p.monto_total > p.monto_pagado ORDER BY saldo DESC""")
    elif tipo == "pendientes":
        cur.execute("""SELECT c.nombre, p.nombre_corto, p.monto_total FROM proyectos p JOIN clientes c ON p.cliente_id = c.id WHERE p.estado = 'Pendiente de cotizar'""")
    elif tipo == "liquidados":
        cur.execute("""SELECT c.nombre, p.nombre_corto, p.monto_total FROM proyectos p JOIN clientes c ON p.cliente_id = c.id WHERE p.estado = 'Liquidado' LIMIT 10""")
    else:
        cur.execute("""SELECT c.nombre, p.nombre_corto, p.monto_total, p.monto_pagado, p.estado FROM proyectos p JOIN clientes c ON p.cliente_id = c.id 
                       WHERE p.estado != 'Cancelado' ORDER BY p.fecha_creacion DESC LIMIT 15""")
    return cur.fetchall()

# 5. MANEJO DE MENSAJES
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['historial'] = []
    context.user_data['cliente_activo'] = ""
    await update.message.reply_text(
        "¡Hola Jefe! 🛠️ Asistente listo.\n\n"
        "Si un cliente no acepta, solo dime 'cancela lo de Don Pedro' y lo marcaré como descartado para que no estorbe."
    )

async def comando_cliente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        cliente_nombre = " ".join(context.args)
        context.user_data['cliente_activo'] = cliente_nombre
        await update.message.reply_text(f"✅ Cliente activo: **{cliente_nombre}**", parse_mode='Markdown')
    else:
        actual = context.user_data.get('cliente_activo', 'Ninguno')
        await update.message.reply_text(f"📌 Cliente activo: **{actual}**", parse_mode='Markdown')

async def procesar_texto(update: Update, context: ContextTypes.DEFAULT_TYPE, texto_original: str):
    try:
        historial = context.user_data.get('historial', [])
        cliente_activo = context.user_data.get('cliente_activo', '')
        
        historial.append(texto_original)
        if len(historial) > 10:
            historial = historial[-10:]
        
        datos_iniciales = analizar_con_ia(texto_original, historial, cliente_activo, "")
        cliente_nombre = datos_iniciales.get("cliente", cliente_activo if cliente_activo else "")
        
        proyectos_existentes_str = ""
        if cliente_nombre and cliente_nombre != "Desconocido":
            conn_temp = get_db_connection()
            cur_temp = conn_temp.cursor()
            proyectos_existentes = obtener_proyectos_activos(cur_temp, cliente_nombre)
            cur_temp.close(); conn_temp.close()
            
            if proyectos_existentes:
                proyectos_existentes_str = "Lista de proyectos activos:\n"
                for p_id, nombre_corto, desc, total, pagado, estado in proyectos_existentes:
                    pendiente = total - pagado
                    proyectos_existentes_str += f"- ID {p_id} | '{nombre_corto}' | Total: ${total} | Pagado: ${pagado} | Pendiente: ${pendiente} | {estado}\n"
        
        datos = analizar_con_ia(texto_original, historial, cliente_activo, proyectos_existentes_str)
        accion = datos.get("accion", "preguntar")
        
        historial.append(f"[Sistema: La IA entendió {accion}]")
        context.user_data['historial'] = historial
        
        if accion == "preguntar":
            pregunta = datos.get("pregunta", "¿Me das más detalles?")
            await update.message.reply_text(f"🤔 {pregunta}")
            return

        if accion == "ignorar_duplicado":
            resumen = datos.get("resumen", "Ya tengo este proyecto registrado.")
            await update.message.reply_text(f"⚠️ {resumen}")
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
                msg = "🔴 **DEUDORES (Pendiente por pagar):**\n\n"
                for n, nc, t, p, s in proyectos: msg += f"👤 {n} ({nc})\n💰 **Debe: ${s:.2f}**\n\n"
            elif tipo == "pendientes":
                msg = "📝 **POR COTIZAR:**\n\n"
                for n, nc, t in proyectos: msg += f" {n} - {nc} (${t:.2f})\n"
            elif tipo == "liquidados":
                msg = "✅ **LIQUIDADOS:**\n\n"
                for n, nc, t in proyectos: msg += f"👤 {n} - {nc} (${t:.2f})\n"
            else:
                msg = "📊 **RESUMEN:**\n\n"
                for n, nc, t, p, e in proyectos: 
                    pendiente = t - p
                    msg += f"👤 {n} | {nc} | Total: ${t} | Pagado: ${p} | Pendiente: ${pendiente} | {e}\n\n"
            await update.message.reply_text(msg, parse_mode='Markdown')
            return

        # Procesar datos finales
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

        if accion == "registrar_proyecto":
            buscar_o_crear_cliente(cur, cliente_nombre, telefono, direccion, notas)
            cliente_id = buscar_o_crear_cliente(cur, cliente_nombre)
            registrar_proyecto(cur, cliente_id, nombre_corto, descripcion, monto, estado, notas)
            respuesta = f"✅ **Nuevo proyecto guardado:**\n{resumen_ia}\n Total: ${monto:.2f}"
            
        elif accion == "actualizar_proyecto":
            buscar_o_crear_cliente(cur, cliente_nombre, telefono, direccion, notas)
            actualizado = actualizar_proyecto(cur, cliente_nombre, nombre_corto, descripcion, monto, estado, notas)
            if actualizado:
                respuesta = f"✏️ **Proyecto actualizado:**\n{resumen_ia}"
            else:
                respuesta = f"⚠️ No encontré proyectos activos para {cliente_nombre}."
            
        elif accion == "cancelar_proyecto":
            actualizado = cancelar_proyecto(cur, cliente_nombre, nombre_corto)
            if actualizado:
                respuesta = f"️ **Proyecto CANCELADO/DESCARTADO:**\n{resumen_ia}\n_(Ya no aparecerá en tus listas de pendientes o deudores)_."
            else:
                respuesta = f"⚠️ No encontré proyectos activos para {cliente_nombre} para cancelar."
            
        elif accion == "registrar_pago":
            buscar_o_crear_cliente(cur, cliente_nombre, telefono, direccion)
            _, msg_pago = registrar_pago(cur, cliente_nombre, monto)
            respuesta = f"💰 **Pago/Anticipo registrado:**\n{resumen_ia}\n{msg_pago}"
            
        elif accion == "registrar_compra":
            registrar_compra(cur, descripcion, monto)
            respuesta = f"🛒 **Compra registrada:**\n{resumen_ia}"
            
        conn.commit()
        cur.close(); conn.close()
        
        respuesta += "\n\n_(Si está mal, dime qué cambiar)_"
        await update.message.reply_text(respuesta, parse_mode='Markdown')

    except Exception as e:
        logging.error(f"🔴 Error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)[:150]}")

async def manejar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text:
        await procesar_texto(update, context, update.message.text)
    elif update.message.voice:
        try:
            await update.message.reply_text("🎙️ Escuchando...")
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
    print("🤖 Bot con sistema de Cancelados y Borrado Lógico iniciado...")
    app.run_polling(drop_pending_updates=True)
