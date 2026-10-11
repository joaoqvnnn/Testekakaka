import os
import json
import sqlite3
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv

import mercadopago
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from aiohttp import web

# Carrega variáveis de ambiente
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MP_ACCESS_TOKEN = os.getenv("MERCADOPAGO_ACCESS_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
CANAL_ID = int(os.getenv("CANAL_ID"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# Configura logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Inicializa SDK do Mercado Pago
sdk = mercadopago.SDK(MP_ACCESS_TOKEN)

# ---------- BANCO DE DADOS (SQLite) ----------
DB_NAME = "bot_database.db"

def init_db():
    """Cria as tabelas necessárias se não existirem."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # Tabela de usuários: user_id (PK), saldo
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            user_id INTEGER PRIMARY KEY,
            saldo REAL DEFAULT 0.0
        )
    """)
    # Tabela de pedidos: payment_id (PK), user_id, produto, valor, status
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pedidos (
            payment_id TEXT PRIMARY KEY,
            user_id INTEGER,
            produto TEXT,
            valor REAL,
            status TEXT,
            data_criacao TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def get_saldo(user_id: int) -> float:
    """Retorna o saldo do usuário. Cria registro se não existir."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT saldo FROM usuarios WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if row is None:
        cursor.execute("INSERT INTO usuarios (user_id, saldo) VALUES (?, 0.0)", (user_id,))
        conn.commit()
        conn.close()
        return 0.0
    conn.close()
    return row[0]

def set_saldo(user_id: int, novo_saldo: float):
    """Define o saldo do usuário."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO usuarios (user_id, saldo) VALUES (?, ?)", (user_id, novo_saldo))
    conn.commit()
    conn.close()

def adicionar_saldo(user_id: int, valor: float):
    """Adiciona valor ao saldo do usuário."""
    saldo_atual = get_saldo(user_id)
    set_saldo(user_id, saldo_atual + valor)

def subtrair_saldo(user_id: int, valor: float) -> bool:
    """Subtrai valor do saldo. Retorna True se bem-sucedido."""
    saldo_atual = get_saldo(user_id)
    if saldo_atual >= valor:
        set_saldo(user_id, saldo_atual - valor)
        return True
    return False

def criar_pedido(payment_id: str, user_id: int, produto: str, valor: float, status: str = "pending"):
    """Registra um novo pedido no banco."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO pedidos (payment_id, user_id, produto, valor, status) VALUES (?, ?, ?, ?, ?)",
        (payment_id, user_id, produto, valor, status),
    )
    conn.commit()
    conn.close()

def atualizar_status_pedido(payment_id: str, novo_status: str):
    """Atualiza o status de um pedido."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE pedidos SET status = ? WHERE payment_id = ?", (novo_status, payment_id))
    conn.commit()
    conn.close()

def obter_pedido(payment_id: str):
    """Retorna os dados de um pedido pelo payment_id."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, produto, valor, status FROM pedidos WHERE payment_id = ?", (payment_id,))
    row = cursor.fetchone()
    conn.close()
    return row

# ---------- MERCADO PAGO: CRIAÇÃO DE PAGAMENTO PIX ----------
def criar_pagamento_pix(valor: float, descricao: str, user_id: int) -> dict:
    """
    Cria uma cobrança PIX no Mercado Pago.
    Retorna o dicionário completo da resposta.
    """
    # Define expiração para 30 minutos
    expire = datetime.now() + timedelta(minutes=30)
    expire_str = expire.strftime("%Y-%m-%dT%H:%M:%S.000-03:00")

    payment_data = {
        "transaction_amount": valor,
        "description": descricao,
        "payment_method_id": "pix",
        "installments": 1,
        "date_of_expiration": expire_str,
        "payer": {
            "email": f"user_{user_id}@teste.com",
            "first_name": "Usuario",
            "last_name": "Teste",
            "identification": {
                "type": "CPF",
                "number": "19119119100"  # CPF fictício apenas para teste
            }
        },
        "notification_url": WEBHOOK_URL
    }

    result = sdk.payment().create(payment_data)
    return result

# ---------- HANDLERS DO TELEGRAM ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start - Mostra o menu principal."""
    user = update.effective_user
    saldo = get_saldo(user.id)

    keyboard = [
        [InlineKeyboardButton("💰 Meu Saldo", callback_data="saldo")],
        [InlineKeyboardButton("🛒 Comprar Produto", callback_data="comprar")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"Olá, {user.first_name}!\n\n"
        f"Seu saldo atual é: **R$ {saldo:.2f}**\n\n"
        f"Use os botões abaixo para navegar.",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )

async def saldo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra o saldo do usuário."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    saldo = get_saldo(user_id)
    await query.edit_message_text(
        f"💰 Seu saldo atual é: **R$ {saldo:.2f}**",
        parse_mode="Markdown",
    )

async def comprar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inicia o fluxo de compra de um produto."""
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("Produto A - R$ 10,00", callback_data="produto_A")],
        [InlineKeyboardButton("Produto B - R$ 25,00", callback_data="produto_B")],
        [InlineKeyboardButton("Voltar", callback_data="voltar")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "🛒 Escolha o produto que deseja comprar:",
        reply_markup=reply_markup,
    )

async def selecionar_produto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processa a seleção de produto e gera o PIX."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    produtos = {
        "produto_A": {"nome": "Produto A", "valor": 10.00},
        "produto_B": {"nome": "Produto B", "valor": 25.00},
    }

    if data not in produtos:
        await query.edit_message_text("Produto inválido.")
        return

    produto = produtos[data]
    valor = produto["valor"]
    nome = produto["nome"]

    # Verifica se o usuário tem saldo suficiente
    saldo = get_saldo(user_id)
    if saldo < valor:
        await query.edit_message_text(
            f"❌ Saldo insuficiente!\n\n"
            f"Seu saldo: R$ {saldo:.2f}\n"
            f"Valor do produto: R$ {valor:.2f}\n\n"
            f"Adicione saldo para continuar.",
        )
        return

    # Cria o pagamento PIX no Mercado Pago
    try:
        result = criar_pagamento_pix(valor, f"Compra - {nome}", user_id)
        if result.get("status") != 201:
            await query.edit_message_text("❌ Erro ao gerar pagamento. Tente novamente.")
            return

        payment = result["response"]
        payment_id = str(payment["id"])
        pix_copia_cola = payment["point_of_interaction"]["transaction_data"]["qr_code"]
        qr_code_base64 = payment["point_of_interaction"]["transaction_data"]["qr_code_base64"]

        # Registra o pedido no banco
        criar_pedido(payment_id, user_id, nome, valor, "pending")

        # Envia o código PIX para o usuário
        await query.edit_message_text(
            f"✅ **Pagamento gerado!**\n\n"
            f"Produto: {nome}\n"
            f"Valor: R$ {valor:.2f}\n\n"
            f"**PIX Copia e Cola:**\n`{pix_copia_cola}`\n\n"
            f"Após o pagamento, o saldo será debitado e o produto será liberado.",
            parse_mode="Markdown",
        )

        # (Opcional) Envia o QR Code como imagem
        # await query.message.reply_photo(photo=BytesIO(base64.b64decode(qr_code_base64)))

    except Exception as e:
        logger.error(f"Erro ao criar pagamento: {e}")
        await query.edit_message_text("❌ Erro interno ao gerar pagamento. Tente novamente.")

async def voltar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Volta ao menu principal."""
    query = update.callback_query
    await query.answer()
    await start(update, context)

# ---------- PAINEL ADMIN ----------
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /admin - Exibe o painel de administração."""
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ Você não tem permissão para acessar este painel.")
        return

    keyboard = [
        [InlineKeyboardButton("➕ Adicionar Saldo", callback_data="admin_add_saldo")],
        [InlineKeyboardButton("➖ Remover Saldo", callback_data="admin_rem_saldo")],
        [InlineKeyboardButton("📊 Estatísticas", callback_data="admin_stats")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "🔧 **Painel de Administração**\n\nEscolha uma opção:",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processa as ações do painel admin."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id != ADMIN_ID:
        await query.edit_message_text("❌ Acesso negado.")
        return

    data = query.data

    if data == "admin_add_saldo":
        await query.edit_message_text(
            "Envie o ID do usuário e o valor no formato:\n`ID VALOR`\nExemplo: `123456789 50.00`",
            parse_mode="Markdown",
        )
        context.user_data["admin_action"] = "add_saldo"

    elif data == "admin_rem_saldo":
        await query.edit_message_text(
            "Envie o ID do usuário e o valor no formato:\n`ID VALOR`\nExemplo: `123456789 20.00`",
            parse_mode="Markdown",
        )
        context.user_data["admin_action"] = "rem_saldo"

    elif data == "admin_stats":
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM usuarios")
        total_users = cursor.fetchone()[0]
        cursor.execute("SELECT SUM(saldo) FROM usuarios")
        total_saldo = cursor.fetchone()[0] or 0.0
        conn.close()
        await query.edit_message_text(
            f"📊 **Estatísticas**\n\n"
            f"👥 Total de usuários: {total_users}\n"
            f"💰 Saldo total em circulação: R$ {total_saldo:.2f}",
            parse_mode="Markdown",
        )

async def admin_processar_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processa a adição/remoção de saldo pelo admin."""
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return

    action = context.user_data.get("admin_action")
    if not action:
        return

    try:
        partes = update.message.text.strip().split()
        if len(partes) != 2:
            raise ValueError("Formato inválido")
        target_id = int(partes[0])
        valor = float(partes[1].replace(",", "."))

        if action == "add_saldo":
            adicionar_saldo(target_id, valor)
            await update.message.reply_text(f"✅ R$ {valor:.2f} adicionados ao usuário {target_id}.")
        elif action == "rem_saldo":
            if subtrair_saldo(target_id, valor):
                await update.message.reply_text(f"✅ R$ {valor:.2f} removidos do usuário {target_id}.")
            else:
                await update.message.reply_text(f"❌ Saldo insuficiente para o usuário {target_id}.")

        context.user_data.pop("admin_action", None)

    except Exception as e:
        await update.message.reply_text("❌ Formato inválido. Use: `ID VALOR`", parse_mode="Markdown")

# ---------- WEBHOOK DO MERCADO PAGO ----------
async def webhook_handler(request):
    """Recebe notificações do Mercado Pago."""
    try pag:
        data = await request.json()
        logger.info(famento"Webhook recebido: {data}")

        # O Mercado P na APIago envia o ID do pagamento na notificação
        if data.get("action") == "payment.created" or data.get("type") == "payment":
            payment_id = data.get("data", {}).get("id")
            if not payment_id:
                return web.Response(status=400)

            # Consulta o do Mercado Pago
            result = sdk.payment().get(payment_id)
            if result.get("status") != 200:
                logger.error(f"Erro ao consultar pagamento {payment_id}")
                return web.Response(status=500)

            payment = result["response"]
            status = payment["status"]

            # Atualiza o pedido no banco
            atualizar_status_pedido(str(payment_id), status)

            if status == "approved":
                pedido = obter_pedido(str(payment_id))
                if pedido:
                    user_id, produto, valor, _ = pedido
                    # Debita o saldo do usuário
                    if subtrair_saldo(user_id, valor):
                        # Notifica o usuário
                        app = Application.builder().token(TELEGRAM_TOKEN).build()
                        await app.bot.send_message(
                            chat_id=user_id,
                            text=f"✅ **Compra aprovada!**\n\n"
                                 f"Produto: {produto}\n"
                                 f"Valor: R$ {valor:.2f}\n\n"
                                 f"Seu saldo foi debitado.",
                            parse_mode="Markdown",
                        )
                        # Notifica o canal
                        await app.bot.send_message(
                            chat_id=CANAL_ID,
                            text=f"🛒 **Nova Compra Realizada!**\n\n"
                                 f"👤 Usuário: `{user_id}`\n"
                                 f"📦 Produto: {produto}\n"
                                 f"💰 Valor: R$ {valor:.2f}\n"
                                 f"🆔 Pagamento: `{payment_id}`",
                            parse_mode="Markdown",
                        )

        return web.Response(status=200)

    except Exception as e:
        logger.error(f"Erro no webhook: {e}")
        return web.Response(status=500)

async def start_webhook_server():
    """Inicia o servidor webhook em paralelo."""
    app = web.Application()
    app.router.add_post("/webhook", webhook_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    logger.info("Servidor webhook rodando na porta 8080")

# ---------- INICIALIZAÇÃO ----------
def main():
    init_db()

    # Cria a aplicação do Telegram
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CallbackQueryHandler(saldo_command, pattern="^saldo$"))
    application.add_handler(CallbackQueryHandler(comprar_command, pattern="^comprar$"))
    application.add_handler(CallbackQueryHandler(selecionar_produto, pattern="^produto_"))
    application.add_handler(CallbackQueryHandler(voltar, pattern="^voltar$"))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern="^admin_"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_processar_saldo))

    # Inicia o servidor webhook em uma thread separada
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(start_webhook_server())

    # Inicia o bot
    logger.info("Bot iniciado. Pressione Ctrl+C para parar.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
