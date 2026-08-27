import os
import json
import logging
import psycopg2
import requests
from groq import Groq
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# 1. CONFIGURACIÓN BÁSICA
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TOKEN = os.environ.get("TELEGRAM_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

groq_client = Groq(api_key=GROQ_API_KEY)

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

# 2. FUNCIÓN DE INTELIGENCIA ARTIFICIAL (GEMINI)
def analizar_con_gemini(texto):
    # Usamos la versión v1 que es la más estable
    url = f"https://generativelanguage.googleapis.com/v1/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    
    prompt = f"""Eres el secretario de un taller de aluminio. Analiza este mensaje y responde SOLO con un objeto JSON válido.
Formato JSON requerido:
{{"cliente": "nombre del cliente o Desconocido", "monto": numero o 0, "descripcion": "resumen breve del trabajo", "estado": "Aceptado" o "Pendiente de cotizar"}}

Mensaje del jefe: "{texto}"

Responde ÚNICAMENTE con el JSON, sin texto extra, sin markdown, sin comillas al inicio."""
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}]
    }
    
    response = requests.post(url, json=payload)
    data = response.json()
    
    # Si Google responde con un error, lo mostramos claramente
    if "candidates" not in data:
        error_msg = data.get("error", {}).get("message", "Error desconocido")
        print(f"ERROR CRÍTICO DE GEMINI: {data}")
        raise Exception(f"La IA falló: {error_msg}")
        
    texto_respuesta = data["candidates"][0]["content"]["parts"][0]["text"]
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

# 4. COMANDOS Y MENSAJES
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "¡Hola! Soy el asistente del taller. 🛠️\n\n"
        "Envíame texto o notas de voz. Ejemplo:\n"
        "'Cobro de 2500 a Don Pedro por el cancel de baño'"
    )

async def procesar_texto(update: Update, texto_original: str):
    try:
        # 1. Analizar con IA
        datos = analizar_con_gemini(texto_original)
        
        cliente_nombre = datos.get("cliente", "Desconocido")
        monto = datos.get("monto", 0)
        descripcion = datos.get("descripcion", texto_original)
        estado = datos.get("estado", "Pendiente de cotizar")

        # 2. Guardar en Base de Datos
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("SELECT id FROM clientes WHERE nombre = %s", (cliente_nombre,))
        cliente = cur.fetchone()
        
        if cliente:
            cliente_id = cliente[0]
        else:
            cur.execute("INSERT INTO clientes (nombre) VALUES (%s) RETURNING id", (cliente_nombre,))
            cliente_id = cur.fetchone()[0]
            
        cur.execute("""
            INSERT INTO proyectos (cliente_id, descripcion, monto_total, estado) 
            VALUES (%s, %s, %s, %s) RETURNING id
        """, (cliente_id, descripcion, monto, estado))
        
        conn.commit()
        cur.close()
        conn.close()
        
        # 3. Responder al usuario
        resumen = (
            f"✅ **¡Anotado y guardado!**\n\n"
            f"👤 *Cliente:* {cliente_nombre}\n"
            f"🔧 *Trabajo:* {descripcion}\n"
            f"💰 *Monto:* ${monto}\n"
            f"📊 *Estado:* {estado}"
        )
        await update.message.reply_text(resumen, parse_mode='Markdown')
        
    except Exception as e:
        logging.error(f"Error: {e}")
        await update.message.reply_text(f" Error: {str(e)}")

async def manejar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Si es texto
    if update.message.text:
        await update.message.reply_text("🧠 Procesando...")
        await procesar_texto(update, update.message.text)
        
    # Si es audio (nota de voz)
    elif update.message.voice:
        try:
            await update.message.reply_text("🎙️ Escuchando y transcribiendo...")
            file = await context.bot.get_file(update.message.voice.file_id)
            ruta_audio = 'voice.ogg'
            await file.download_to_drive(ruta_audio)
            
            texto_transcrito = transcribir_audio(ruta_audio)
            os.remove(ruta_audio) # Borrar archivo temporal
            
            await update.message.reply_text(f" *Transcripción:* \"{texto_transcrito}\"", parse_mode='Markdown')
            await procesar_texto(update, texto_transcrito)
            
        except Exception as e:
            logging.error(f"Error de audio: {e}")
            await update.message.reply_text(f" Error con audio: {str(e)}")

# 5. INICIO DEL BOT
if __name__ == '__main__':
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT | filters.VOICE, manejar_mensaje))
    
    print("🤖 Bot iniciado con Gemini (REST), Groq y DB...")
    app.run_polling()
