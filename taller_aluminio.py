import os
import json
import logging
import psycopg2
import google.generativeai as genai
from groq import Groq
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# Configuración
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# Variables de entorno
TOKEN = os.environ.get("TELEGRAM_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

# Configurar IAs
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel('gemini-1.5-flash')
groq_client = Groq(api_key=GROQ_API_KEY)

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "¡Hola! Soy el asistente del taller. 🛠️\n\n"
        "Puedes enviarme *texto* o *notas de voz*. Ejemplo:\n"
        "'Cobro de 2500 a Don Pedro por el cancel de baño'"
    )

def analizar_con_gemini(texto):
    prompt = f"""
    Eres el secretario de un taller de aluminio. Analiza el siguiente mensaje y extrae la información en formato JSON estricto.
    Campos requeridos:
    - "cliente": Nombre del cliente (si no hay, pon "Desconocido").
    - "monto": Número del monto total o cobro (si no hay, pon 0).
    - "descripcion": Breve resumen del trabajo o cobro.
    - "estado": "Aceptado" si es un cobro/trabajo real, "Pendiente de cotizar" si solo piden precio.
    
    Mensaje del jefe: "{texto}"
    
    Responde SOLO con el JSON, sin texto extra ni markdown.
    """
    response = gemini_model.generate_content(prompt)
    texto_limpio = response.text.replace("```json", "").replace("```", "").strip()
    return json.loads(texto_limpio)

def transcribir_audio(ruta_archivo):
    with open(ruta_archivo, "rb") as file:
        transcription = groq_client.audio.transcriptions.create(
            file=(ruta_archivo, file.read()),
            model="whisper-large-v3",
            language="es"
        )
    return transcription.text

async def procesar_texto(update: Update, texto_original: str):
    try:
        await update.message.reply_text("🧠 Procesando...")
        datos = analizar_con_gemini(texto_original)
        
        cliente_nombre = datos.get("cliente", "Desconocido")
        monto = datos.get("monto", 0)
        descripcion = datos.get("descripcion", texto_original)
        estado = datos.get("estado", "Pendiente de cotizar")

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
        
        proyecto_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        
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
        await update.message.reply_text(f"❌ Hubo un error al procesar: {e}")

async def manejar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Si es texto
    if update.message.text:
        await procesar_texto(update, update.message.text)
    
    # Si es audio (nota de voz)
    elif update.message.voice:
        try:
            await update.message.reply_text("🎙️ Escuchando y transcribiendo...")
            file = await context.bot.get_file(update.message.voice.file_id)
            ruta_audio = 'voice.ogg'
            await file.download_to_drive(ruta_audio)
            
            texto_transcrito = transcribir_audio(ruta_audio)
            os.remove(ruta_audio) # Borrar el archivo temporal
            
            await update.message.reply_text(f"📝 *Transcripción:* \"{texto_transcrito}\"", parse_mode='Markdown')
            await procesar_texto(update, texto_transcrito)
            
        except Exception as e:
            logging.error(f"Error de audio: {e}")
            await update.message.reply_text("❌ No pude transcribir el audio. Intenta de nuevo.")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT | filters.VOICE, manejar_mensaje))
    
    print("🤖 Bot iniciado con Cerebro (Gemini), Oídos (Groq) y Memoria (DB)...")
    app.run_polling()
