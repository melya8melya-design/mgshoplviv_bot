import os
import logging
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MS_BASE = "https://api.moysklad.ru/api/remap/1.2"
ASK_NAME, BROWSE, ADD_QTY, ADD_COMMENT = range(4)
user_data_store = {}


def ms_headers():
    return {
        "Authorization": f"Bearer {os.environ.get('MOYSKLAD_TOKEN', '')}",
        "Content-Type": "application/json",
        "Accept-Encoding": "gzip"
    }


def get_stock_with_folders():
    try:
        price_type_id = os.environ.get("PRICE_TYPE_ID", "9bfd4d89-d17f-11e9-0a80-05370019bfc8")

        # Залишки
        stock_resp = requests.get(
            f"{MS_BASE}/report/stock/all?filter=stockMode=nonEmpty&limit=1000",
            headers=ms_headers(), timeout=20
        )
        stock_resp.raise_for_status()
        stock_items = stock_resp.json().get("rows", [])

        # Збираємо ID товарів з ненульовим залишком
        stock_map = {}
        for item in stock_items:
            if item.get("stock", 0) <= 0:
                continue
            pid = item.get("assortmentId", item.get("id", ""))
            stock_map[pid] = {
                "name": item.get("name", ""),
                "stock": int(item.get("stock", 0)),
                "folder_href": item.get("folder", {}).get("meta", {}).get("href", ""),
            }

        # Групи товарів
        folders_resp = requests.get(
            f"{MS_BASE}/entity/productfolder?limit=100",
            headers=ms_headers(), timeout=15
        )
        folders_resp.raise_for_status()
        folders = {f["id"]: f["name"] for f in folders_resp.json().get("rows", [])}

        # Ціни продажу через /entity/product (посторінково)
        price_map = {}
        offset = 0
        total = 9999
        while offset < total:
            p_resp = requests.get(
                f"{MS_BASE}/entity/product?limit=100&offset={offset}",
                headers=ms_headers(), timeout=30
            )
            p_resp.raise_for_status()
            data = p_resp.json()
            total = data.get("meta", {}).get("size", 0)
            rows = data.get("rows", [])
            if not rows:
                break
            for prod in rows:
                pid = prod.get("id", "")
                if pid not in stock_map:
                    continue
                sale_prices = prod.get("salePrices", [])
                price = 0
                for sp in sale_prices:
                    if sp.get("priceType", {}).get("id", "") == price_type_id:
                        price = sp.get("value", 0) / 100
                        break
                price_map[pid] = price
            offset += 100

        # Збираємо каталог
        catalog = {}
        for pid, info in stock_map.items():
            folder_href = info["folder_href"]
            folder_id = folder_href.split("/")[-1] if folder_href else ""
            folder_name = folders.get(folder_id, "Інше")
            top_folder = folder_name.split("/")[0]

            if top_folder not in catalog:
                catalog[top_folder] = []

            catalog[top_folder].append({
                "id": pid,
                "name": info["name"],
                "stock": info["stock"],
                "price": price_map.get(pid, 0),
            })

        return catalog
    except Exception as e:
        logger.error(f"Помилка отримання каталогу: {e}")
        return {}


def create_order_in_moysklad(user_name, cart, comment):
    try:
        org_id = os.environ.get("ORGANIZATION_ID", "")
        positions = []
        for item in cart:
            positions.append({
                "quantity": item["qty"],
                "price": int(item["price"] * 100),
                "assortment": {
                    "meta": {
                        "href": f"{MS_BASE}/entity/product/{item['id']}",
                        "type": "product",
                        "mediaType": "application/json"
                    }
                }
            })

        order_data = {
            "name": f"Замовлення від {user_name}",
            "description": comment if comment and comment != "-" else "",
            "organization": {
                "meta": {
                    "href": f"{MS_BASE}/entity/organization/{org_id}",
                    "type": "organization",
                    "mediaType": "application/json"
                }
            },
            "positions": positions
        }

        resp = requests.post(
            f"{MS_BASE}/entity/customerorder",
            headers=ms_headers(),
            json=order_data,
            timeout=15
        )
        resp.raise_for_status()
        result = resp.json()
        return result.get("name", ""), result.get("id", "")
    except Exception as e:
        logger.error(f"Помилка створення замовлення: {e}")
        return None, None


def get_user(user_id):
    if user_id not in user_data_store:
        user_data_store[user_id] = {"name": None, "cart": [], "catalog": {}, "folder_index": {}}
    return user_data_store[user_id]


def format_cart(cart):
    if not cart:
        return "Кошик порожній"
    lines = []
    total = 0
    for i, item in enumerate(cart, 1):
        subtotal = item["price"] * item["qty"]
        total += subtotal
        lines.append(f"{i}. {item['name']} — {item['qty']} ящ. × {item['price']:.0f} грн = {subtotal:.0f} грн")
    lines.append(f"\n💰 Разом: {total:.0f} грн")
    return "\n".join(lines)


def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Каталог", callback_data="catalog")],
        [InlineKeyboardButton("🛒 Кошик", callback_data="cart")],
        [InlineKeyboardButton("✅ Оформити замовлення", callback_data="checkout")],
    ])


def folders_keyboard(catalog, folder_index):
    buttons = []
    for idx, folder in enumerate(sorted(catalog.keys())):
        folder_index[str(idx)] = folder
        count = len(catalog[folder])
        buttons.append([InlineKeyboardButton(f"{folder} ({count})", callback_data=f"f:{idx}")])
    buttons.append([InlineKeyboardButton("🛒 Кошик", callback_data="cart")])
    return InlineKeyboardMarkup(buttons)


def products_keyboard(products, folder_idx):
    buttons = []
    for i, p in enumerate(products):
        name = p['name'][:35] if len(p['name']) > 35 else p['name']
        price_str = f"{p['price']:.0f}грн" if p['price'] > 0 else "ціна не вказана"
        buttons.append([InlineKeyboardButton(
            f"{name} | {price_str} | {p['stock']}ящ",
            callback_data=f"p:{i}:{folder_idx}"
        )])
    buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="catalog")])
    buttons.append([InlineKeyboardButton("🛒 Кошик", callback_data="cart")])
    return InlineKeyboardMarkup(buttons)


def cart_keyboard(cart):
    buttons = []
    for i, item in enumerate(cart):
        name = item['name'][:30] if len(item['name']) > 30 else item['name']
        buttons.append([InlineKeyboardButton(f"❌ {name}", callback_data=f"rm:{i}")])
    buttons.append([InlineKeyboardButton("📦 Продовжити вибір", callback_data="catalog")])
    if cart:
        buttons.append([InlineKeyboardButton("✅ Оформити замовлення", callback_data="checkout")])
    buttons.append([InlineKeyboardButton("🏠 Меню", callback_data="menu")])
    return InlineKeyboardMarkup(buttons)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if user["name"]:
        await update.message.reply_text(
            f"👋 З поверненням, {user['name']}!",
            reply_markup=main_menu_keyboard()
        )
        return BROWSE
    await update.message.reply_text(
        "👋 Вітаємо! Це бот для оформлення замовлень.\n\nЯк вас звати або як називається ваша компанія?"
    )
    return ASK_NAME


async def ask_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    name = update.message.text.strip()
    get_user(user_id)["name"] = name
    await update.message.reply_text(
        f"✅ Дякуємо, {name}! Оберіть що хочете зробити:",
        reply_markup=main_menu_keyboard()
    )
    return BROWSE


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    user = get_user(user_id)
    data = query.data

    if data == "menu":
        await query.edit_message_text(
            f"Привіт, {user['name']}! Оберіть дію:",
            reply_markup=main_menu_keyboard()
        )
        return BROWSE

    if data == "catalog":
        await query.edit_message_text("⏳ Завантажую каталог...")
        catalog = get_stock_with_folders()
        user["catalog"] = catalog
        user["folder_index"] = {}
        if not catalog:
            await query.edit_message_text(
                "😔 Каталог порожній або помилка підключення.",
                reply_markup=main_menu_keyboard()
            )
            return BROWSE
        await query.edit_message_text(
            "📦 Оберіть групу товарів:",
            reply_markup=folders_keyboard(catalog, user["folder_index"])
        )
        return BROWSE

    if data.startswith("f:"):
        idx = data.split(":")[1]
        folder = user["folder_index"].get(idx)
        if not folder:
            await query.edit_message_text("Помилка. Спробуйте ще раз.", reply_markup=main_menu_keyboard())
            return BROWSE
        products = user["catalog"].get(folder, [])
        if not products:
            await query.edit_message_text("Товарів у цій групі немає.", reply_markup=folders_keyboard(user["catalog"], user["folder_index"]))
            return BROWSE
        await query.edit_message_text(
            f"📂 {folder}\n\nОберіть товар:",
            reply_markup=products_keyboard(products, idx)
        )
        return BROWSE

    if data.startswith("p:"):
        parts = data.split(":")
        product_idx = int(parts[1])
        folder_idx = parts[2]
        folder = user["folder_index"].get(folder_idx)
        products = user["catalog"].get(folder, []) if folder else []
        if not products or product_idx >= len(products):
            await query.edit_message_text("Товар не знайдено.", reply_markup=main_menu_keyboard())
            return BROWSE
        found = products[product_idx]
        context.user_data["selected_product"] = found
        price_str = f"{found['price']:.0f} грн/ящ." if found['price'] > 0 else "ціна уточнюється"
        await query.edit_message_text(
            f"🛒 *{found['name']}*\n"
            f"Ціна: {price_str}\n"
            f"В наявності: {found['stock']} ящ.\n\n"
            f"Введіть кількість ящиків:",
            parse_mode="Markdown"
        )
        return ADD_QTY

    if data == "cart":
        cart = user["cart"]
        text = f"🛒 Ваш кошик:\n\n{format_cart(cart)}"
        await query.edit_message_text(text, reply_markup=cart_keyboard(cart))
        return BROWSE

    if data.startswith("rm:"):
        idx = int(data.split(":")[1])
        if 0 <= idx < len(user["cart"]):
            removed = user["cart"].pop(idx)
            await query.edit_message_text(
                f"❌ Видалено: {removed['name']}\n\n🛒 Кошик:\n\n{format_cart(user['cart'])}",
                reply_markup=cart_keyboard(user["cart"])
            )
        return BROWSE

    if data == "checkout":
        if not user["cart"]:
            await query.edit_message_text(
                "🛒 Кошик порожній. Спочатку додайте товари.",
                reply_markup=main_menu_keyboard()
            )
            return BROWSE
        await query.edit_message_text(
            f"🛒 Ваше замовлення:\n\n{format_cart(user['cart'])}\n\n"
            f"💬 Додайте коментар до замовлення\n(або напишіть «-» якщо не потрібен):"
        )
        return ADD_COMMENT

    if data == "confirm":
        comment = context.user_data.get("comment", "")
        order_name, order_id = create_order_in_moysklad(user["name"], user["cart"], comment)
        if order_id:
            cart_summary = format_cart(user["cart"])
            success_text = f"✅ Замовлення прийнято!\n\n👤 Клієнт: {user['name']}\n{cart_summary}"
            if comment and comment != "-":
                success_text += f"\n\n💬 Коментар: {comment}"
            await query.edit_message_text(success_text)

            manager_text = f"🔔 *Нове замовлення!*\n\n👤 Клієнт: {user['name']}\n\n{cart_summary}"
            if comment and comment != "-":
                manager_text += f"\n\n💬 Коментар: {comment}"
            manager_text += f"\n\n📋 МійСклад: {order_name}"

            for manager_id in os.environ.get("MANAGER_CHAT_IDS", "").split(","):
                if manager_id.strip():
                    try:
                        await context.bot.send_message(
                            chat_id=int(manager_id.strip()),
                            text=manager_text,
                            parse_mode="Markdown"
                        )
                    except Exception as e:
                        logger.error(f"Помилка надсилання менеджеру: {e}")
            user["cart"] = []
        else:
            await query.edit_message_text(
                "❌ Помилка створення замовлення. Спробуйте ще раз.",
                reply_markup=main_menu_keyboard()
            )
        return BROWSE

    if data == "cancel_order":
        await query.edit_message_text("Замовлення скасовано.", reply_markup=main_menu_keyboard())
        return BROWSE

    return BROWSE


async def handle_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    text = update.message.text.strip()
    try:
        qty = int(text)
        if qty <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Введіть ціле число більше 0:")
        return ADD_QTY

    product = context.user_data.get("selected_product")
    if not product:
        await update.message.reply_text("Помилка. Спробуйте ще раз.", reply_markup=main_menu_keyboard())
        return BROWSE

    if qty > product["stock"]:
        await update.message.reply_text(f"⚠️ В наявності тільки {product['stock']} ящ. Введіть іншу кількість:")
        return ADD_QTY

    for item in user["cart"]:
        if item["id"] == product["id"]:
            item["qty"] += qty
            await update.message.reply_text(
                f"✅ Оновлено: {product['name']} — тепер {item['qty']} ящ.",
                reply_markup=main_menu_keyboard()
            )
            return BROWSE

    user["cart"].append({
        "id": product["id"],
        "name": product["name"],
        "price": product["price"],
        "qty": qty
    })
    await update.message.reply_text(
        f"✅ Додано: {product['name']} — {qty} ящ.\n\n🛒 В кошику: {len(user['cart'])} позицій",
        reply_markup=main_menu_keyboard()
    )
    return BROWSE


async def handle_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    comment = update.message.text.strip()
    context.user_data["comment"] = comment

    cart_text = format_cart(user["cart"])
    confirm_text = f"📋 Підтвердіть замовлення:\n\n{cart_text}"
    if comment and comment != "-":
        confirm_text += f"\n\n💬 Коментар: {comment}"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Підтвердити", callback_data="confirm")],
        [InlineKeyboardButton("❌ Скасувати", callback_data="cancel_order")],
    ])
    await update.message.reply_text(confirm_text, reply_markup=keyboard)
    return BROWSE


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Скасовано.", reply_markup=main_menu_keyboard())
    return BROWSE


def main():
    app = Application.builder().token(os.environ.get("TELEGRAM_TOKEN", "")).build()
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_name)],
            BROWSE: [
                CallbackQueryHandler(handle_callback),
                CommandHandler("start", start),
            ],
            ADD_QTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_qty)],
            ADD_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_comment)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )
    app.add_handler(conv_handler)
    logger.info("Бот запущено")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
