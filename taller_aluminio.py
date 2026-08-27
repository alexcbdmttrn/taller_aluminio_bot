import os
import logging
import psycopg2
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# Configuración
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# Obtener variables de entorno (Railway las inyecta automáticamente)
TOKEN = os.environ.get("TELEGRAM_TOKEN", "8840922230:AAFCxbf-sorjOab6K17QG1IAuX66ekEEke0")
DATABASE_URL = os.environ.get("DATABASE_URL")

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "¡Hola! Soy el asistente del taller. 🛠️\n\n"
        "Prueba enviándome un mensaje como:\n"
        "'Cobro de 2000 a Don Pedro por el cancel de baño'\n\n"
        "Por ahora lo guardaré como una nota. ¡En el siguiente paso la IA lo entenderá!"
    )

async def responder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text
    
    # Si es un audio, por ahora le decimos que aún no está activo
    if not texto:
        await update.message.reply_text("🎙️ Audio recibido. La transcripción con IA se activará en el próximo paso.")
        return

    try:
        # 1. Conectar a la base de datos
        conn = get_db_connection()
        cur = conn.cursor()
        
        # 2. Guardar el mensaje en la tabla de proyectos (como nota temporal)
        cur.execute("""
            INSERT INTO proyectos (descripcion, estado, monto_total) 
            VALUES (%s, 'Nota sin procesar', 0)
            RETURNING id
        """, (texto,))
        
        nuevo_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        
        # 3. Responder al usuario
        await update.message.reply_text(
            f"✅ **¡Guardado en la base de datos!** (Registro #{nuevo_id})\n\n"
            f"📝 *Nota:* '{texto}'\n\n"
            f"_(Próximo paso: Conectar la IA para que extraiga automáticamente el nombre, monto y proyecto)_."
        )
        
    except Exception as e:
        logging.error(f"Error de base de datos: {e}")
        await update.message.reply_text("❌ Hubo un error al guardar. Revisa los registros de Railway.")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    # Solo responde a texto, ignora comandos como /start
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, responder))
    
    print("🤖 Bot iniciado y conectado a la base de datos...")
    app.run_polling()
