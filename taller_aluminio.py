import os
import json
import logging
import psycopg2
import google.generativeai as genai
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# Configuración
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# Variables de entorno
TOKEN = os.environ.get("TELEGRAM_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Configurar Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "¡Hola! Soy el asistente del taller. 🛠️\n\n"
        "Dime qué hiciste o qué te pidieron. Ejemplo:\n"
        "'Cobro de 2500 a Don Pedro por el cancel de baño'"
    )

def procesar_con_ia(texto):
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
    response = model.generate_content(prompt)
    # Limpiar la respuesta por si Gemini agrega ```json ... ```
    texto_limpio = response.text.replace("```json", "").replace("```", "").strip()
    return json.loads(texto_limpio)

async def responder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text
    if not texto:
        await update.message.reply_text("🎙️ Audio recibido. (La transcripción se activará en el siguiente paso).")
        return

    try:
        # 1. Pedirle a la IA que entienda el mensaje
        await update.message.reply_text("🧠 Pensando...")
        datos = procesar_con_ia(texto)
        
        cliente_nombre = datos.get("cliente", "Desconocido")
        monto = datos.get("monto", 0)
        descripcion = datos.get("descripcion", texto)
        estado = datos.get("estado", "Pendiente de cotizar")

        # 2. Guardar en la Base de Datos
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Buscar o crear cliente
        cur.execute("SELECT id FROM clientes WHERE nombre = %s", (cliente_nombre,))
        cliente = cur.fetchone()
        
        if cliente:
            cliente_id = cliente[0]
        else:
            cur.execute("INSERT INTO clientes (nombre) VALUES (%s) RETURNING id", (cliente_nombre,))
            cliente_id = cur.fetchone()[0]
            
        # Insertar proyecto
        cur.execute("""
            INSERT INTO proyectos (cliente_id, descripcion, monto_total, estado) 
            VALUES (%s, %s, %s, %s) RETURNING id
        """, (cliente_id, descripcion, monto, estado))
        
        proyecto_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        
        # 3. Responder con resumen
        resumen = (
            f"✅ **¡Anotado y guardado!**\n\n"
            f" *Cliente:* {cliente_nombre}\n"
            f"🔧 *Trabajo:* {descripcion}\n"
            f"💰 *Monto:* ${monto}\n"
            f"📊 *Estado:* {estado}"
        )
        await update.message.reply_text(resumen, parse_mode='Markdown')
        
    except Exception as e:
        logging.error(f"Error: {e}")
        await update.message.reply_text(f"❌ Hubo un error al procesar: {e}")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, responder))
    
    print("🤖 Bot iniciado con Cerebro IA y Memoria DB...")
    app.run_polling()
