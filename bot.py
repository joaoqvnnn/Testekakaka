import os
import logging
import asyncio
import sqlite3
from datetime import datetime, timedelta
from dotenv import load_dotenv

import mercadopago
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)
from aiohttp import web

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MP_ACCESS_TOKEN = os.getenv("MERCADOPAGO_ACCESS_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CANAL_ID = int(os.getenv("CANAL_ID", "0"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://testekakaka.onrender.com")
PORT = int(os.environ.get("PORT", 8080))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

sdk = mercadopago.SDK(MP_ACCESS_TOKEN)

DB_NAME = "bot_database.db"

# Estados de conversa
(ADD_SALDO_VALOR, ADD_PRODUTO_NOME, ADD_PRODUTO_PRECO,
 REM_SALDO_VALOR, REM_PRODUTO_ID) = range(5)


# ============================================================
# BANCO DE DADOS
# ============================================================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            user_id INTEGER PRIMARY KEY,
            saldo REAL DEFAULT 0.0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS pedidos (
            payment_id TEXT PRIMARY KEY,
            user_id INTEGER,
            produto TEXT,
            valor REAL,
            status TEXT,
            data_criacao TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS produtos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            preco REAL NOT NULL,
            ativo INTEGER DEFAULT 1
        )
    """)
    conn.commit()
    conn.close()


def get_saldo(user_id):
    conn = sqlite3.connect(DB_NAME); c = conn.cursor()
    c.execute("SELECT saldo FROM usuarios WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if row is None:
        c.execute("INSERT INTO usuarios (user_id, saldo) VALUES (?, 0.0)", (user_id,))
        conn.commit(); conn.close(); return 0.0
    conn.close(); return row[0]


def set_saldo(user_id, novo_saldo):
    conn = sqlite3.connect(DB_NAME); c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO usuarios (user_id, saldo) VALUES (?, ?)", (user_id, novo_saldo))
    conn.commit(); conn.close()


def adicionar_saldo(user_id, valor):
    set_saldo(user_id, get_saldo(user_id) + valor)


def subtrair_saldo(user_id, valor):
    s = get_saldo(user_id)
    if s >= valor:
        set_saldo(user_id, s - valor); return True
    return False


def criar_pedido(payment_id, user_id, produto, valor, status="pending"):
    conn = sqlite3.connect(DB_NAME); c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO pedidos (payment_id, user_id, produto, valor, status) VALUES (?, ?, ?, ?, ?)",
              (payment_id, user_id, produto, valor, status))
    conn.commit(); conn.close()


def atualizar_status_pedido(payment_id, novo_status):
    conn = sqlite3.connect(DB_NAME); c = conn.cursor()
    c.execute("UPDATE pedidos SET status = ? WHERE payment_id = ?", (novo_status, payment_id))
    conn.commit(); conn.close()


def obter_pedido(payment_id):
    conn = sqlite3.connect(DB_NAME); c = conn.cursor()
    c.execute("SELECT user_id, produto, valor, status FROM pedidos WHERE payment_id = ?", (payment_id,))
    row = c.fetchone(); conn.close(); return row


def listar_produtos():
    conn = sqlite3.connect(DB_NAME); c = conn.cursor()
    c.execute("SELECT id, nome, preco FROM produtos WHERE ativo = 1 ORDER BY id")
    rows = c.fetchall(); conn.close(); return rows


def obter_produto(produto_id):
    conn = sqlite3.connect(DB_NAME); c = conn.cursor()
    c.execute("SELECT id, nome, preco FROM produtos WHERE id = ? AND ativo = 1", (produto_id,))
    row = c.fetchone(); conn.close(); return row


def criar_produto(nome, preco):
    conn = sqlite3.connect(DB_NAME); c = conn.cursor()
    c.execute("INSERT INTO produtos (nome, preco) VALUES (?, ?)", (nome, preco))
    conn.commit(); conn.close()


def desativar_produto(produto_id):
    conn = sqlite3.connect(DB_NAME); c = conn.cursor()
    c.execute("UPDATE produtos SET ativo = 0 WHERE id = ?", (produto_id,))
    conn.commit(); conn.close()


def formatar_preco(v):
    return f"R$ {v:.2f}".replace(".", ",")


# ============================================================
# MERCADO PAGO
# ============================================================
def criar_pagamento_pix(valor, descricao, user_id):
    expire = datetime.now() + timedelta(minutes=30)
    expire_str = expire.strftime("%Y-%m-%dT%H:%M:%S.000-03:00")
    payment_data = {
        "transaction_amount": float(valor),
        "description": descricao,
        "payment_method_id": "pix",
        "installments": 1,
        "date_of_expiration": expire_str,
        "payer": {
            "email": f"user_{user_id}@teste.com",
            "first_name": "Usuario",
            "last_name": "Teste",
            "identification": {"type": "CPF", "number": "19119119100"},
        },
        "notification_url": f"{WEBHOOK_URL}/webhook",
    }
    return sdk.payment().create(payment_data)


# ============================================================
# HANDLERS DO USUÁRIO
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    saldo = get_saldo(user.id)
    keyboard = [
        [InlineKeyboardButton("💰 Meu Saldo", callback_data="saldo")],
        [InlineKeyboardButton("🛒 Comprar Produto", callback_data="comprar")],
    ]
    texto = (f"Olá, {user.first_name}!\n\n"
             f"Seu saldo atual é: *{formatar_preco(saldo)}*\n\n"
             f"Use os botões abaixo para navegar.")
    if update.callback_query:
        await update.callback_query.edit_message_text(texto, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.message.reply_text(texto, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def saldo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    saldo = get_saldo(q.from_user.id)
    await q.edit_message_text(f"💰 Seu saldo: *{formatar_preco(saldo)}*", parse_mode="Markdown")


async def comprar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    produtos = listar_produtos()
    if not produtos:
        await q.edit_message_text("❌ Nenhum produto disponível no momento.")
        return
    keyboard = []
    for pid, nome, preco in produtos:
        keyboard.append([InlineKeyboardButton(f"{nome} — {formatar_preco(preco)}", callback_data=f"buy_{pid}")])
    keyboard.append([InlineKeyboardButton("Voltar", callback_data="voltar")])
    await q.edit_message_text("🛒 Escolha um produto:", reply_markup=InlineKeyboardMarkup(keyboard))


async def selecionar_produto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    user_id = q.from_user.id
    pid = int(q.data.replace("buy_", ""))
    produto = obter_produto(pid)
    if not produto:
        await q.edit_message_text("❌ Produto não disponível.")
        return
    _, nome, valor = produto
    saldo = get_saldo(user_id)
    if saldo < valor:
        await q.edit_message_text(
            f"❌ *Saldo insuficiente!*\n\n"
            f"Seu saldo: {formatar_preco(saldo)}\n"
            f"Valor do produto: {formatar_preco(valor)}\n\n"
            f"Adicione saldo com o admin para continuar.",
            parse_mode="Markdown")
        return
    try:
        result = criar_pagamento_pix(valor, f"Compra - {nome}", user_id)
        if result.get("status") not in (200, 201):
            logger.error(f"Erro MP: {result}")
            await q.edit_message_text("❌ Erro ao gerar pagamento. Tente novamente.")
            return
        payment = result["response"]
        payment_id = str(payment["id"])
        pix = payment.get("point_of_interaction", {}).get("transaction_data", {}).get("qr_code", "")
        criar_pedido(payment_id, user_id, nome, valor, "pending")
        await q.edit_message_text(
            f"✅ *Pagamento gerado!*\n\n"
            f"Produto: {nome}\n"
            f"Valor: {formatar_preco(valor)}\n\n"
            f"*PIX Copia e Cola:*\n`{pix}`\n\n"
            f"Após o pagamento, o saldo será debitado.",
            parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Erro ao criar pagamento: {e}")
        await q.edit_message_text("❌ Erro interno. Tente novamente.")


async def voltar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


# ============================================================
# PAINEL ADMIN
# ============================================================
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Sem permissão.")
        return
    keyboard = [
        [InlineKeyboardButton("➕ Adicionar Saldo", callback_data="admin_add_saldo")],
        [InlineKeyboardButton("➖ Remover Saldo", callback_data="admin_rem_saldo")],
        [InlineKeyboardButton("🆕 Cadastrar Produto", callback_data="admin_add_prod")],
        [InlineKeyboardButton("🗑️ Remover Produto", callback_data="admin_rem_prod")],
        [InlineKeyboardButton("📦 Listar Produtos", callback_data="admin_list_prod")],
        [InlineKeyboardButton("📊 Estatísticas", callback_data="admin_stats")],
    ]
    await update.message.reply_text(
        "🔧 *Painel de Administração*\n\nEscolha uma opção:",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.from_user.id != ADMIN_ID:
        await q.edit_message_text("❌ Acesso negado."); return
    data = q.data

    if data == "admin_add_saldo":
        await q.edit_message_text("Envie no formato:\n`ID_USUARIO VALOR`\nExemplo: `8939269687 0.01`", parse_mode="Markdown")
        return ADD_SALDO_VALOR
    elif data == "admin_rem_saldo":
        await q.edit_message_text("Envie no formato:\n`ID_USUARIO VALOR`\nExemplo: `8939269687 5.00`", parse_mode="Markdown")
        return REM_SALDO_VALOR
    elif data == "admin_add_prod":
        await q.edit_message_text("Qual o *nome* do produto?", parse_mode="Markdown")
        return ADD_PRODUTO_NOME
    elif data == "admin_rem_prod":
        produtos = listar_produtos()
        if not produtos:
            await q.edit_message_text("❌ Nenhum produto cadastrado."); return ConversationHandler.END
        lista = "\n".join([f"`{pid}` — {nome} ({formatar_preco(preco)})" for pid, nome, preco in produtos])
        await q.edit_message_text(f"Envie o *ID* do produto para remover:\n\n{lista}", parse_mode="Markdown")
        return REM_PRODUTO_ID
    elif data == "admin_list_prod":
        produtos = listar_produtos()
        if not produtos:
            await q.edit_message_text("📦 Nenhum produto cadastrado."); return ConversationHandler.END
        lista = "\n".join([f"`{pid}` — {nome} — {formatar_preco(preco)}" for pid, nome, preco in produtos])
        await q.edit_message_text(f"📦 *Produtos ativos:*\n\n{lista}", parse_mode="Markdown")
        return ConversationHandler.END
    elif data == "admin_stats":
        conn = sqlite3.connect(DB_NAME); c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM usuarios"); total_users = c.fetchone()[0]
        c.execute("SELECT SUM(saldo) FROM usuarios"); total_saldo = c.fetchone()[0] or 0.0
        c.execute("SELECT COUNT(*) FROM produtos WHERE ativo = 1"); total_prod = c.fetchone()[0]
        conn.close()
        await q.edit_message_text(
            f"📊 *Estatísticas*\n\n"
            f"👥 Usuários: {total_users}\n"
            f"💰 Saldo em circulação: {formatar_preco(total_saldo)}\n"
            f"📦 Produtos ativos: {total_prod}",
            parse_mode="Markdown")
        return ConversationHandler.END


async def processar_add_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        partes = update.message.text.strip().split()
        target = int(partes[0])
        valor = float(partes[1].replace(",", "."))
        if valor <= 0: raise ValueError
        adicionar_saldo(target, valor)
        await update.message.reply_text(f"✅ {formatar_preco(valor)} adicionados ao usuário `{target}`.", parse_mode="Markdown")
    except Exception:
        await update.message.reply_text("❌ Formato inválido. Use: `ID_USUARIO VALOR`", parse_mode="Markdown")
    return ConversationHandler.END


async def processar_rem_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        partes = update.message.text.strip().split()
        target = int(partes[0])
        valor = float(partes[1].replace(",", "."))
        if valor <= 0: raise ValueError
        if subtrair_saldo(target, valor):
            await update.message.reply_text(f"✅ {formatar_preco(valor)} removidos do usuário `{target}`.", parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ Saldo insuficiente.")
    except Exception:
        await update.message.reply_text("❌ Formato inválido. Use: `ID_USUARIO VALOR`", parse_mode="Markdown")
    return ConversationHandler.END


async def processar_prod_nome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["novo_prod_nome"] = update.message.text.strip()
    await update.message.reply_text("Agora envie o *preço* (ex: `0.01`, `1.50`, `100`):", parse_mode="Markdown")
    return ADD_PRODUTO_PRECO


async def processar_prod_preco(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        preco = float(update.message.text.strip().replace(",", "."))
        if preco <= 0: raise ValueError
    except Exception:
        await update.message.reply_text("❌ Preço inválido. Envie um número (ex: `0.01`).", parse_mode="Markdown")
        return ADD_PRODUTO_PRECO
    nome = context.user_data.get("novo_prod_nome", "Produto")
    criar_produto(nome, preco)
    await update.message.reply_text(f"✅ Produto *{nome}* cadastrado por {formatar_preco(preco)}!", parse_mode="Markdown")
    context.user_data.pop("novo_prod_nome", None)
    return ConversationHandler.END


async def processar_rem_prod(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        pid = int(update.message.text.strip())
        produto = obter_produto(pid)
        if not produto:
            await update.message.reply_text("❌ Produto não encontrado."); return ConversationHandler.END
        desativar_produto(pid)
        await update.message.reply_text(f"✅ Produto *{produto[1]}* removido.", parse_mode="Markdown")
    except Exception:
        await update.message.reply_text("❌ Envie apenas o ID numérico.", parse_mode="Markdown")
    return ConversationHandler.END


async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Operação cancelada.")
    return ConversationHandler.END


# ============================================================
# WEBHOOK
# ============================================================
async def webhook_handler(request):
    try:
        data = await request.json()
        logger.info(f"Webhook recebido: {data}")

        if "update_id" in data:
            app: Application = request.app["telegram_app"]
            update = Update.de_json(data, app.bot)
            await app.process_update(update)
            return web.Response(status=200)

        payment_id = None
        if data.get("type") == "payment" or str(data.get("action", "")).startswith("payment"):
            payment_id = data.get("data", {}).get("id")

        if payment_id:
            result = sdk.payment().get(payment_id)
            if result.get("status") == 200:
                payment = result["response"]
                status = payment["status"]
                atualizar_status_pedido(str(payment_id), status)
                if status == "approved":
                    pedido = obter_pedido(str(payment_id))
                    if pedido:
                        uid, prod, val, _ = pedido
                        if subtrair_saldo(uid, val):
                            app: Application = request.app["telegram_app"]
                            try:
                                await app.bot.send_message(uid,
                                    f"✅ *Compra aprovada!*\n\nProduto: {prod}\nValor: {formatar_preco(val)}\n\nSaldo debitado.",
                                    parse_mode="Markdown")
                            except Exception as e:
                                logger.error(f"Erro msg user: {e}")
                            try:
                                await app.bot.send_message(CANAL_ID,
                                    f"🛒 *Nova Compra!*\n\n👤 `{uid}`\n📦 {prod}\n💰 {formatar_preco(val)}\n🆔 `{payment_id}`",
                                    parse_mode="Markdown")
                            except Exception as e:
                                logger.error(f"Erro msg canal: {e}")
        return web.Response(status=200)
    except Exception as e:
        logger.error(f"Erro webhook: {e}")
        return web.Response(status=500)


async def healthcheck(request):
    return web.Response(text="OK")


# ============================================================
# MAIN
# ============================================================
async def main_async():
    init_db()
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_callback, pattern="^admin_")],
        states={
            ADD_SALDO_VALOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, processar_add_saldo)],
            REM_SALDO_VALOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, processar_rem_saldo)],
            ADD_PRODUTO_NOME: [MessageHandler(filters.TEXT & ~filters.COMMAND, processar_prod_nome)],
            ADD_PRODUTO_PRECO: [MessageHandler(filters.TEXT & ~filters.COMMAND, processar_prod_preco)],
            REM_PRODUTO_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, processar_rem_prod)],
        },
        fallbacks=[CommandHandler("cancel", cancelar)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(conv)
    application.add_handler(CallbackQueryHandler(saldo_command, pattern="^saldo$"))
    application.add_handler(CallbackQueryHandler(comprar_command, pattern="^comprar$"))
    application.add_handler(CallbackQueryHandler(selecionar_produto, pattern="^buy_"))
    application.add_handler(CallbackQueryHandler(voltar, pattern="^voltar$"))

    await application.initialize()
    await application.start()
    await application.bot.set_webhook(url=f"{WEBHOOK_URL}/webhook", allowed_updates=Update.ALL_TYPES)
    logger.info(f"Webhook configurado: {WEBHOOK_URL}/webhook")

    app = web.Application()
    app["telegram_app"] = application
    app.router.add_post("/webhook", webhook_handler)
    app.router.add_get("/", healthcheck)
    app.router.add_get("/healthcheck", healthcheck)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logger.info(f"Servidor rodando na porta {PORT}")

    await asyncio.Event().wait()


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
