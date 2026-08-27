import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# Configurar registros para ver errores
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# Tu Token de Telegram
TOKEN = "8840922230:AAFCxbf-sorjOab6K17QG1IAuX66ekEEke0"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("¡Hola! Soy el asistente del taller. Envíame un mensaje o un audio para empezar.")

async def responder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Por ahora solo responde lo que recibe
    texto = update.message.text or "Audio recibido 🎙️"
    await update.message.reply_text(f"✅ Recibido: '{texto}'. (Aún no guardo en la base de datos, pero ya te escucho).")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT | filters.VOICE, responder))
    
    print("🤖 Bot iniciado...")
    app.run_polling()
