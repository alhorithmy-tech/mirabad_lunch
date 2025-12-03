import sqlite3
import aiosqlite
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.error import NetworkError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackContext,
    CallbackQueryHandler,
)
import logging
from pathlib import Path
import asyncio
import httpx
from datetime import datetime

import configparser

import threading
from aiohttp import web
import aiohttp
import asyncio
import json

# Чтение конфигурации
config = configparser.ConfigParser()
try:
    config.read("config.ini")
    TOKEN = str(config["Settings"]["BOT_TOKEN"]).strip(" \"'")  # Удаляем все кавычки
    ADMIN_ID = int(
        str(config["Settings"]["ADMIN_ID"]).strip(" \"'")
    )  # Удаляем кавычки и преобразуем в число
except Exception as e:
    print("⛔ ОШИБКА В config.ini ⛔")
    print("Правильный формат файла:")
    print("[Settings]")
    print("BOT_TOKEN = ваш_токен_без_кавычек")
    print("ADMIN_ID = ваш_id_без_кавычек")
    print(f"Ошибка: {e}")
    input("Нажмите Enter для выхода...")
    exit()
ADMIN_IDS = [ADMIN_ID]  # Список админов

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    filename="food_bot.log",
)
logger = logging.getLogger(__name__)

# Пути
BASE_DIR = Path(__file__).parent
# Пути для Mini App
MINI_APP_DIR = BASE_DIR / "mini_app"
DB_PATH = BASE_DIR / "food_bot.db"


# Чтение конфигурации
config = configparser.ConfigParser()
try:
    config.read("config.ini")
    TOKEN = str(config["Settings"]["BOT_TOKEN"]).strip(" \"'")
    ADMIN_ID = int(str(config["Settings"]["ADMIN_ID"]).strip(" \"'"))
    ADMIN_IDS = [ADMIN_ID]  # Теперь берётся только из config.ini!
except Exception as e:
    print("⛔ Ошибка в config.ini!")
    print(f"Проверьте, что файл существует и содержит:")
    print("[Settings]")
    print("BOT_TOKEN = ваш_токен")
    print("ADMIN_ID = ваш_id")
    print(f"Ошибка: {e}")
    input("Нажмите Enter для выхода...")
    exit()

ADMIN_ORDER_STATUSES = ["В обработке", "Готовится", "В пути", "Доставлен"]

# Включение/отключение функции оценки заказа
REVIEWS_ENABLED = True  # Флаг для включения/отключения системы отзывов. Для отключения установить False


# Модифицируем функцию admin_change_status:
async def admin_change_status(update: Update, context: CallbackContext):
    if context.user_data.get("admin_action") != "order_control":
        return

    new_status = update.message.text
    if new_status not in ADMIN_ORDER_STATUSES:
        return

    order_id = context.user_data.get("selected_order_id")
    if not order_id:
        await update.message.reply_text("❌ Не выбран заказ")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE orders SET status = ? WHERE id = ?", (new_status, order_id)
        )
        await db.commit()

        cursor = await db.execute(
            "SELECT user_id FROM orders WHERE id = ?", (order_id,)
        )
        user_row = await cursor.fetchone()
        await cursor.close()

        if user_row:
            user_id = user_row[0]
            status_emoji = {
                "В обработке": "🔄",
                "Готовится": "🧑‍🍳",
                "В пути": "🚚",
                "Доставлен": "✅",
            }.get(new_status, "")

            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"""
🔔 *Статус вашего заказа #{order_id} изменён:*
{status_emoji} *{new_status}*

Спасибо, что выбрали нас! 😊
                    """,
                    parse_mode="Markdown",
                )

                if new_status == "Доставлен" and REVIEWS_ENABLED:
                    await ask_for_review(context, user_id, order_id)

            except Exception as e:
                logger.error(f"Ошибка отправки уведомления: {e}")

    await update.message.reply_text(
        f"✅ Статус заказа #{order_id} обновлён на: *{new_status}*",
        parse_mode="Markdown",
        reply_markup=admin_main_keyboard(),
    )
    context.user_data.pop("admin_action", None)
    context.user_data.pop("selected_order_id", None)


# Состояния бота
STATE_MAIN_MENU, STATE_CATEGORIES, STATE_DISHES, STATE_CART = range(4)


# ========== MINI APP WEB SERVER ==========
async def serve_mini_app(request):
    """Отдает файлы Mini App"""
    path = request.match_info.get("path", "index.html")
    file_path = MINI_APP_DIR / path

    # Логируем запрос
    logger.debug(f"Mini App запрос: {path}")

    if not file_path.exists():
        logger.warning(f"Файл не найден: {file_path}")
        return web.Response(text="File not found", status=404)

    return web.FileResponse(file_path)


async def api_get_menu(request):
    """API для получения меню для Mini App"""
    try:
        logger.info("Mini App: запрос меню")

        async with aiosqlite.connect(DB_PATH) as db:
            # Категории
            cursor = await db.execute(
                "SELECT id, name, emoji FROM categories ORDER BY id"
            )
            categories = await cursor.fetchall()

            # Блюда
            cursor = await db.execute(
                """
                SELECT d.id, d.name, d.description, d.price, c.name as category_name, d.image_path 
                FROM dishes d 
                JOIN categories c ON d.category_id = c.id
                ORDER BY c.id, d.id
            """
            )
            dishes = await cursor.fetchall()

        # Формируем данные для Mini App
        menu_data = {
            "categories": [
                {"id": c[0], "name": c[1], "emoji": c[2] or ""} for c in categories
            ],
            "dishes": [
                {
                    "id": d[0],
                    "name": d[1],
                    "description": d[2] or "",
                    "price": float(d[3]),
                    "category": d[4],
                    "image_url": f"https://{request.host}/static/{d[5]}",
                }
                for d in dishes
            ],
        }

        logger.info(
            f"Mini App: отправлено {len(categories)} категорий, {len(dishes)} блюд"
        )
        return web.json_response(menu_data)

    except Exception as e:
        logger.error(f"Mini App ошибка получения меню: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def api_create_order(request):
    """API для создания заказа из Mini App"""
    try:
        data = await request.json()
        logger.info(f"Mini App: получен заказ {data}")

        # Здесь будет логика создания заказа
        # Пока просто возвращаем успех
        return web.json_response(
            {
                "success": True,
                "message": "Заказ получен! Вернитесь в бота для завершения.",
                "order_id": 999,  # Временный ID
            }
        )

    except Exception as e:
        logger.error(f"Mini App ошибка создания заказа: {e}")
        return web.json_response({"success": False, "error": str(e)})


async def serve_static(request):
    """Отдает статические файлы (картинки)"""
    path = request.match_info.get("path", "")
    file_path = BASE_DIR / path

    # Логируем запрос
    logger.debug(f"Static file request: {path} -> {file_path}")

    if not file_path.exists() or not file_path.is_file():
        logger.warning(f"Static file not found: {file_path}")
        return web.Response(text="File not found", status=404)

    return web.FileResponse(file_path)


def start_web_server():
    """Запуск веб-сервера для Mini App"""
    try:
        # Создаем приложение
        app = web.Application()

        # Добавляем CORS для работы с Telegram
        async def cors_middleware(app, handler):
            async def middleware_handler(request):
                if request.method == "OPTIONS":
                    response = web.Response()
                else:
                    response = await handler(request)

                response.headers["Access-Control-Allow-Origin"] = "*"
                response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
                response.headers["Access-Control-Allow-Headers"] = "Content-Type"
                return response

            return middleware_handler

        app.middlewares.append(cors_middleware)

        # API endpoints
        app.router.add_get("/api/menu", api_get_menu)
        app.router.add_post("/api/order", api_create_order)
        app.router.add_options("/api/order", api_create_order)

        # Статические файлы Mini App
        app.router.add_get("/static/{path:.*}", serve_static)
        app.router.add_get("/", serve_mini_app)
        app.router.add_get("/{path:.*}", serve_mini_app)

        # Запускаем веб-сервер
        logger.info("🔄 Запуск веб-сервера для Mini App на http://localhost:8080")
        web.run_app(app, host="localhost", port=8080, access_log=None)

    except Exception as e:
        logger.error(f"Ошибка веб-сервера: {e}")


def start_web_server_thread():
    """Запускает веб-сервер в отдельном потоке"""
    try:
        # Проверяем существование папки mini_app
        if not MINI_APP_DIR.exists():
            logger.warning(f"Папка {MINI_APP_DIR} не найдена! Создаю...")
            MINI_APP_DIR.mkdir(exist_ok=True)

        # Запускаем в отдельном потоке
        thread = threading.Thread(target=start_web_server, daemon=True)
        thread.start()
        logger.info("✅ Веб-сервер Mini App запущен в фоновом режиме")

    except Exception as e:
        logger.error(f"Ошибка запуска веб-сервера: {e}")


async def api_create_order(request):
    """API для создания заказа из Mini App"""
    try:
        data = await request.json()
        logger.info(f"Mini App: получен заказ {data}")

        # Извлекаем данные
        items = data["items"]
        total = data["total"]
        address = data.get("address", "")
        phone = data.get("phone", "")
        comment = data.get("comment", "")

        # Здесь создаем заказ в БД (аналогично обычному заказу)
        # Пока заглушка - всегда успех
        order_id = 999  # Заглушка

        # Уведомление админу
        order_text = f"🛒 ЗАКАЗ ИЗ MINI APP #{order_id}\n\n"
        order_text += f"📍 Адрес: {address}\n"
        order_text += f"📞 Телефон: {phone}\n"
        if comment:
            order_text += f"💬 Комментарий: {comment}\n"
        order_text += f"💰 Сумма: {total} сум\n\n"
        order_text += "🍽 Состав:\n" + "\n".join(
            f"• {item['name']} - {item['quantity']} шт." for item in items
        )

        # Отправляем уведомление админу (существующим методом)
        # await context.bot.send_message(ADMIN_ID, order_text) - нужно доработать

        return web.json_response(
            {
                "success": True,
                "message": "Заказ принят! Мы свяжемся с вами.",
                "order_id": order_id,
            }
        )

    except Exception as e:
        logger.error(f"Mini App ошибка создания заказа: {e}")
        return web.json_response({"success": False, "error": str(e)})


# ========== БАЗА ДАННЫХ ==========
def init_db():
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                emoji TEXT
            )"""
            )

            cursor.execute(
                """
            CREATE TABLE IF NOT EXISTS dishes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER,
                name TEXT NOT NULL,
                description TEXT,
                image_path TEXT,
                price REAL NOT NULL,
                FOREIGN KEY (category_id) REFERENCES categories(id)
            )"""
            )

            cursor.execute(
                """
            CREATE TABLE IF NOT EXISTS carts (
                user_id INTEGER NOT NULL,
                dish_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (user_id, dish_id),
                FOREIGN KEY (dish_id) REFERENCES dishes(id)
            )"""
            )

            cursor.execute(
                """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                order_date TEXT NOT NULL,
                total_amount REAL NOT NULL,
                status TEXT NOT NULL,
                comment TEXT DEFAULT ''
            )"""
            )

            cursor.execute(
                """
            CREATE TABLE IF NOT EXISTS order_items (
                order_id INTEGER NOT NULL,
                dish_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                price REAL NOT NULL,
                FOREIGN KEY (order_id) REFERENCES orders(id),
                FOREIGN KEY (dish_id) REFERENCES dishes(id)
            )"""
            )

            cursor.execute(
                """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                phone TEXT,
                name TEXT
            )"""
            )

            cursor.execute(
                """
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                order_id INTEGER NOT NULL,
                rating INTEGER CHECK(rating BETWEEN 1 AND 5),
                comment TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (order_id) REFERENCES orders(id)
            )"""
            )

            cursor.execute("SELECT COUNT(*) FROM categories")
            if cursor.fetchone()[0] == 0:
                cursor.executemany(
                    "INSERT INTO categories (name, emoji) VALUES (?, ?)",
                    [
                        ("Полуфабрикаты", "🍛"),
                        ("Готовые блюда", "🍲"),
                        ("Десерты", "🍰"),
                        ("Салаты", "🥗"),
                    ],
                )
                cursor.executemany(
                    """INSERT INTO dishes (category_id, name, description, image_path, price)
                    VALUES (?, ?, ?, ?, ?)""",
                    [
                        (
                            1,
                            "Голубцы",
                            "С говядиной, 500 г",
                            "bot_images/semi_finished/golubcy.jpg",
                            350,
                        ),
                        (
                            1,
                            "Тефтели",
                            "Из говядины, 400 г",
                            "bot_images/semi_finished/tefteli.jpg",
                            300,
                        ),
                        (
                            2,
                            "Пицца",
                            "Маргарита, 30 см",
                            "bot_images/ready_meals/pizza.jpg",
                            450,
                        ),
                        (
                            3,
                            "Медовик",
                            "Торт, 1000 г",
                            "bot_images/desserts/medovik.jpg",
                            800,
                        ),
                    ],
                )
                conn.commit()
        logger.info("База данных инициализирована")
    except Exception as e:
        logger.error(f"Ошибка инициализации БД: {e}")


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def get_categories():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT id, name, emoji FROM categories")
        return await cursor.fetchall()


async def get_dishes_by_category(category_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT id, name, description, image_path, price 
            FROM dishes 
            WHERE category_id = ?""",
            (category_id,),
        )
        return await cursor.fetchall()


async def get_dish_details(dish_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT d.id, d.name, d.description, d.image_path, d.price, c.name 
            FROM dishes d
            JOIN categories c ON d.category_id = c.id
            WHERE d.id = ?""",
            (dish_id,),
        )
        return await cursor.fetchone()


async def add_to_cart(user_id, dish_id, quantity=1):
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                """
                INSERT INTO carts (user_id, dish_id, quantity) 
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, dish_id) 
                DO UPDATE SET quantity = quantity + ?""",
                (user_id, dish_id, quantity, quantity),
            )
            await db.commit()
            return True
        except Exception as e:
            logger.error(f"Ошибка добавления в корзину: {e}")
            return False


async def get_cart_items(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT d.id, d.name, d.price, c.quantity 
            FROM carts c
            JOIN dishes d ON c.dish_id = d.id
            WHERE c.user_id = ?""",
            (user_id,),
        )
        return await cursor.fetchall()


async def clear_cart(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM carts WHERE user_id = ?", (user_id,))
        await db.commit()


async def create_order(user_id: int, total_amount: float, comment: str = "") -> int:
    """Создает новый заказ в базе данных"""
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            # Проверяем существование пользователя
            cursor = await db.execute("SELECT 1 FROM users WHERE id = ?", (user_id,))
            user_exists = await cursor.fetchone()
            await cursor.close()

            if not user_exists:
                logger.warning(f"Пользователь {user_id} не найден в таблице users")
                try:
                    await db.execute("INSERT INTO users (id) VALUES (?)", (user_id,))
                    await db.commit()
                except Exception as e:
                    logger.error(f"Ошибка создания пользователя: {e}")

            # Создаем заказ
            order_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor = await db.execute(
                """
                INSERT INTO orders (user_id, order_date, total_amount, status, comment)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, order_date, float(total_amount), "В обработке", comment),
            )
            order_id = cursor.lastrowid
            await db.commit()
            return order_id

        except Exception as e:
            logger.error(f"Ошибка при создании заказа: {e}")
            raise


async def add_order_items(order_id, items):
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            for item in items:
                await db.execute(
                    """
                    INSERT INTO order_items (order_id, dish_id, quantity, price)
                    VALUES (?, ?, ?, ?)""",
                    (int(order_id), int(item[0]), int(item[3]), float(item[2])),
                )
            await db.commit()
        except Exception as e:
            logger.error(f"Ошибка при добавлении позиций заказа: {e}")
            raise


async def get_order_history(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT id, order_date, total_amount, status 
            FROM orders 
            WHERE user_id = ?
            ORDER BY order_date DESC""",
            (user_id,),
        )
        return await cursor.fetchall()


async def get_order_details(order_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT d.name, oi.quantity, oi.price
            FROM order_items oi
            JOIN dishes d ON oi.dish_id = d.id
            WHERE oi.order_id = ?""",
            (order_id,),
        )
        return await cursor.fetchall()


# ========== ОТЗЫВЫ ==========
async def ask_for_review(context: CallbackContext, user_id: int, order_id: int):
    """Запрашивает отзыв у пользователя с inline-кнопками"""
    try:
        # Проверяем, не оставлял ли уже пользователь отзыв на этот заказ
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT 1 FROM reviews WHERE order_id = ?", (order_id,)
            )
            review_exists = await cursor.fetchone()
            await cursor.close()

            if review_exists:
                logger.info(
                    f"Пользователь {user_id} уже оставлял отзыв на заказ {order_id}"
                )
                return

        keyboard = [
            [
                InlineKeyboardButton("⭐", callback_data=f"rate_{order_id}_1"),
                InlineKeyboardButton("⭐⭐", callback_data=f"rate_{order_id}_2"),
                InlineKeyboardButton("⭐⭐⭐", callback_data=f"rate_{order_id}_3"),
            ],
            [
                InlineKeyboardButton("⭐⭐⭐⭐", callback_data=f"rate_{order_id}_4"),
                InlineKeyboardButton("⭐⭐⭐⭐⭐", callback_data=f"rate_{order_id}_5"),
            ],
            [
                InlineKeyboardButton(
                    "❌ Не хочу оставлять отзыв", callback_data=f"rate_{order_id}_0"
                )
            ],
        ]

        await context.bot.send_message(
            chat_id=user_id,
            text=f"🍽 Как вам заказ #{order_id}?\nОцените наше обслуживание:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except Exception as e:
        logger.error(f"Ошибка при запросе отзыва: {e}")


async def handle_review_callback(update: Update, context: CallbackContext):
    """Обрабатывает нажатие inline-кнопок с оценкой"""
    query = update.callback_query
    await query.answer()

    _, order_id, rating = query.data.split("_")
    order_id = int(order_id)
    rating = int(rating)
    user_id = query.from_user.id

    if rating == 0:
        # Удаляем клавиатуру при отказе от отзыва
        await query.edit_message_text(
            text="Спасибо за заказ! Приятного аппетита!",
            reply_markup=None,  # Удаляем inline-клавиатуру
        )
        return

    # Сохраняем оценку и запрашиваем комментарий
    context.user_data[f"review_{order_id}"] = {"rating": rating}

    # Отправляем новое сообщение с запросом комментария
    await context.bot.send_message(
        chat_id=user_id,
        text="📝 Напишите ваш отзыв (или нажмите 'Пропустить'):",
        reply_markup=ReplyKeyboardMarkup([["Пропустить"]], resize_keyboard=True),
    )

    # Удаляем предыдущее сообщение с кнопками
    try:
        await query.delete_message()
    except Exception as e:
        logger.error(f"Ошибка при удалении сообщения: {e}")

    context.user_data["state"] = "AWAITING_REVIEW_COMMENT"


async def save_review(update: Update, context: CallbackContext):
    """Сохраняет отзыв в БД"""
    user_id = update.effective_user.id
    text = update.message.text

    # Находим последний заказ, для которого запрашивался отзыв
    order_id = None
    for key in context.user_data:
        if key.startswith("review_"):
            order_id = int(key.split("_")[1])
            break

    if not order_id:
        await update.message.reply_text("Не удалось определить заказ для отзыва")
        return

    rating = context.user_data[f"review_{order_id}"]["rating"]
    comment = None if text.lower() == "пропустить" else text

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            # Проверяем существование заказа
            cursor = await db.execute(
                "SELECT 1 FROM orders WHERE id = ? AND user_id = ?", (order_id, user_id)
            )
            order_exists = await cursor.fetchone()
            await cursor.close()

            if not order_exists:
                await update.message.reply_text("❌ Заказ не найден")
                return

            # Проверяем, не оставлял ли уже пользователь отзыв на этот заказ
            cursor = await db.execute(
                "SELECT 1 FROM reviews WHERE order_id = ?", (order_id,)
            )
            review_exists = await cursor.fetchone()
            await cursor.close()

            if review_exists:
                await update.message.reply_text(
                    "❌ Вы уже оставляли отзыв на этот заказ"
                )
                return

            # Сохраняем отзыв
            await db.execute(
                "INSERT INTO reviews (user_id, order_id, rating, comment) VALUES (?, ?, ?, ?)",
                (user_id, order_id, rating, comment),
            )
            await db.commit()

        # Уведомление админа
        admin_msg = f"⭐ Новый отзыв!\nЗаказ: #{order_id}\nОценка: {'⭐' * rating}"
        if comment:
            admin_msg += f"\nОтзыв: {comment}"

        try:
            await context.bot.send_message(ADMIN_ID, admin_msg)
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления админу: {e}")

        await update.message.reply_text(
            "✅ Спасибо за отзыв!", reply_markup=main_menu_keyboard()
        )

    except sqlite3.OperationalError as e:
        logger.error(f"Ошибка базы данных при сохранении отзыва: {e}")
        await update.message.reply_text("❌ Ошибка базы данных. Попробуйте позже.")
    except Exception as e:
        logger.error(f"Неожиданная ошибка при сохранении отзыва: {e}")
        await update.message.reply_text("❌ Не удалось сохранить отзыв")

    # Очистка
    context.user_data.pop(f"review_{order_id}", None)
    context.user_data["state"] = STATE_MAIN_MENU


# ========== ОБРАБОТЧИКИ КОМАНД ==========
def main_menu_keyboard():
    return ReplyKeyboardMarkup(
        [["🍽 Меню"], ["🛒 Корзина", "📋 История заказов"]], resize_keyboard=True
    )


def admin_main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["📊 Статистика", "📦 Активные заказы"],
            ["🔍 Найти заказ", "🔙 Выйти из админки"],
        ],
        resize_keyboard=True,
    )


def admin_orders_keyboard():
    return ReplyKeyboardMarkup(
        [*[[status] for status in ADMIN_ORDER_STATUSES], ["🔙 Назад"]],
        resize_keyboard=True,
    )


async def start(update: Update, context: CallbackContext):
    user = update.effective_user
    await update.message.reply_text(
        f"Привет, {user.first_name}!\nЯ бот для заказа еды. Используйте кнопки меню:",
        reply_markup=main_menu_keyboard(),
    )

    # Кнопка для Mini App (улучшенное меню)
    inline_keyboard = [
        [
            InlineKeyboardButton(
                "📱 Улучшенное меню",
                web_app={"url": "https://alhorithmy-tech.github.io/mirabad_lunch/"},
            )
        ]
    ]

    await update.message.reply_text(
        "✨ *Новая функция!* Удобное меню с картинками:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard),
        parse_mode="Markdown",
    )

    context.user_data["state"] = STATE_MAIN_MENU


async def show_categories(update: Update, context: CallbackContext):
    categories = await get_categories()
    keyboard = []
    for i in range(0, len(categories), 2):
        row = []
        if i < len(categories):
            row.append(f"{categories[i][2]} {categories[i][1]}")
        if i + 1 < len(categories):
            row.append(f"{categories[i+1][2]} {categories[i+1][1]}")
        keyboard.append(row)
    keyboard.append(["🔙 Назад"])

    await update.message.reply_text(
        "Выберите категорию:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
    )
    context.user_data["state"] = STATE_CATEGORIES


async def show_dishes(update: Update, context: CallbackContext):
    category_name = update.message.text.split(" ", 1)[1]
    categories = await get_categories()

    category_id = None
    for cat in categories:
        if cat[1] == category_name:
            category_id = cat[0]
            break

    if category_id is None:
        await update.message.reply_text("Категория не найдена")
        return

    dishes = await get_dishes_by_category(category_id)
    if not dishes:
        await update.message.reply_text("В этой категории пока нет блюд")
        return

    keyboard = []
    for i in range(0, len(dishes), 2):
        row = []
        if i < len(dishes):
            row.append(dishes[i][1])
        if i + 1 < len(dishes):
            row.append(dishes[i + 1][1])
        keyboard.append(row)

    keyboard.append(["🔙 Назад"])

    await update.message.reply_text(
        f"Блюда в категории {category_name}:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
    )
    context.user_data["state"] = STATE_DISHES
    context.user_data["current_category"] = category_id


async def show_dish_details(
    update: Update, context: CallbackContext, edit_message: bool = False
):
    if "current_category" not in context.user_data:
        await update.message.reply_text("Ошибка: категория не выбрана")
        return

    if update.message.text in ["➕ Увеличить", "➖ Уменьшить"]:
        dish_id = context.user_data.get("current_dish")
        if not dish_id:
            return
    else:
        dish_name = update.message.text
        dishes = await get_dishes_by_category(context.user_data["current_category"])
        dish_id = None
        for dish in dishes:
            if dish[1] == dish_name:
                dish_id = dish[0]
                break

    if not dish_id:
        await update.message.reply_text("Блюдо не найдено")
        return

    dish = await get_dish_details(dish_id)
    if not dish:
        await update.message.reply_text("Информация о блюде недоступна")
        return

    if "quantity" not in context.user_data:
        context.user_data["quantity"] = 1

    quantity = context.user_data["quantity"]
    keyboard = [
        ["➖ Уменьшить", f"Количество: {quantity}", "➕ Увеличить"],
        ["✅ Добавить в корзину"],
        ["🔙 Назад в категории"],
    ]

    total_price = dish[4] * quantity
    caption = f"🍽 {dish[1]}\n\n📝 {dish[2]}\n\n💰 Цена: {dish[4]} сум × {quantity} = {total_price} сум"

    try:
        if edit_message:
            await context.bot.edit_message_reply_markup(
                chat_id=update.effective_chat.id,
                message_id=update.effective_message.message_id,
                reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
            )
            await context.bot.edit_message_caption(
                chat_id=update.effective_chat.id,
                message_id=update.effective_message.message_id,
                caption=caption,
            )
        else:
            image_path = BASE_DIR / dish[3]
            if image_path.exists():
                with open(image_path, "rb") as photo:
                    await update.message.reply_photo(
                        photo=InputFile(photo),
                        caption=caption,
                        reply_markup=ReplyKeyboardMarkup(
                            keyboard, resize_keyboard=True
                        ),
                    )
            else:
                await update.message.reply_text(
                    caption,
                    reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
                )
    except Exception as e:
        logger.error(f"Ошибка при обновлении сообщения: {e}")
        await update.message.reply_text(
            caption, reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )

    context.user_data["current_dish"] = dish_id


async def handle_quantity_change(update: Update, context: CallbackContext):
    if "current_dish" not in context.user_data:
        return

    text = update.message.text
    if text == "➕ Увеличить":
        context.user_data["quantity"] = context.user_data.get("quantity", 1) + 1
    elif text == "➖ Уменьшить":
        if context.user_data.get("quantity", 1) > 1:
            context.user_data["quantity"] -= 1

    await show_dish_details(update, context, edit_message=True)


async def add_to_cart_handler(update: Update, context: CallbackContext):
    if "current_dish" not in context.user_data:
        await update.message.reply_text("Ошибка: блюдо не выбрано")
        return

    user_id = update.effective_user.id
    dish_id = context.user_data["current_dish"]
    quantity = context.user_data.get("quantity", 1)

    if await add_to_cart(user_id, dish_id, quantity):
        cart_items = await get_cart_items(user_id)
        total = sum(item[2] * item[3] for item in cart_items)

        message = f"✅ {quantity} шт. добавлено в корзину!\n\n"
        message += f"🛒 В корзине {len(cart_items)} позиций на сумму {total} сум"

        await update.message.reply_text(
            message,
            reply_markup=ReplyKeyboardMarkup(
                [["🔙 Назад в категории"], ["🛒 Корзина"]], resize_keyboard=True
            ),
        )
        context.user_data["quantity"] = 1
    else:
        await update.message.reply_text("❌ Не удалось добавить блюдо в корзину")


async def show_cart(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    cart_items = await get_cart_items(user_id)
    context.user_data["state"] = STATE_CART

    if not cart_items:
        await update.message.reply_text(
            "🛒 Ваша корзина пуста", reply_markup=main_menu_keyboard()
        )
        return

    total = 0
    message = "🛒 Ваша корзина:\n\n"
    for item in cart_items:
        item_total = item[2] * item[3]
        total += item_total
        message += f"• {item[1]} - {item[3]} шт. × {item[2]} сум = {item_total} сум\n"

    message += f"\n💵 Итого: {total} сум"

    keyboard = [["✅ Оформить заказ"], ["🗑 Очистить корзину"], ["🔙 Назад в категории"]]

    await update.message.reply_text(
        message, reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )


async def checkout(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    cart_items = await get_cart_items(user_id)

    if not cart_items:
        await update.message.reply_text("Ваша корзина пуста!")
        return

    # Сохраняем данные корзины
    context.user_data["pending_order"] = {
        "cart_items": cart_items,
        "total": sum(item[2] * item[3] for item in cart_items),
    }
    keyboard = [[KeyboardButton("📍 Отправить местоположение", request_location=True)]]
    await update.message.reply_text(
        "📦 *Укажите адрес доставки:*\n\n"
        "1. Нажмите кнопку ниже для отправки геолокации\n"
        "2. Или просто напишите адрес в чат (например: _ул. Ленина, 42, кв. 5_)\n\n"
        "⚠️ Для кнопки включите геолокацию на телефоне",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        parse_mode="Markdown",
    )
    context.user_data["state"] = "AWAITING_ADDRESS"


async def handle_contact(update: Update, context: CallbackContext):
    if context.user_data.get("state") != "AWAITING_PHONE":
        return

    contact = update.message.contact
    if not contact:
        await update.message.reply_text("Пожалуйста, поделитесь номером телефона.")
        return

    phone_number = contact.phone_number
    user_id = update.effective_user.id
    user = update.effective_user

    # Сохраняем/обновляем данные пользователя
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (id, phone, name) 
            VALUES (?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET 
                phone = excluded.phone,
                name = excluded.name
            """,
            (user_id, phone_number, user.full_name),
        )
        await db.commit()

    # Получаем данные о заказе из временных данных
    order_data = context.user_data.get("pending_order")
    if not order_data:
        await update.message.reply_text(
            "Произошла ошибка при обработке заказа. Пожалуйста, попробуйте снова.",
            reply_markup=main_menu_keyboard(),
        )
        return

    # Создаем клавиатуру для подтверждения
    confirm_keyboard = ReplyKeyboardMarkup(
        [["✅ Подтвердить заказ"], ["❌ Отменить"]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    # Формируем сообщение для подтверждения
    order_message = "📋 Подтвердите ваш заказ:\n\n"
    for item in order_data["cart_items"]:
        order_message += f"• {item[1]} - {item[3]} шт. × {item[2]} сум\n"

    order_message += f"\n💰 Итого: {order_data['total']} сум"
    order_message += f"\n\n📱 Ваш телефон: {phone_number}"

    if "comment" in order_data:
        order_message += f"\n📝 Комментарий: {order_data['comment']}"

    await update.message.reply_text(order_message, reply_markup=confirm_keyboard)

    # Сохраняем номер телефона и переходим в состояние подтверждения
    context.user_data["pending_order"]["phone"] = phone_number
    context.user_data["state"] = "AWAITING_CONFIRMATION"


### === НОВЫЙ ОБРАБОТЧИК ГЕОЛОКАЦИИ === ###
"""async def handle_location(update: Update, context: CallbackContext):
    if context.user_data.get("state") not in ["AWAITING_COMMENT", "AWAITING_PHONE"]:
        return

    location = update.message.location
    if not location:
        await update.message.reply_text("Пожалуйста, отправьте ваше местоположение.")
        return

    # Сохраняем координаты в данных заказа
    if "pending_order" not in context.user_data:
        context.user_data["pending_order"] = {}
    
    context.user_data["pending_order"]["location"] = {
        "latitude": location.latitude,
        "longitude": location.longitude
    }

    # Переходим к следующему шагу
    if context.user_data.get("state") == "AWAITING_COMMENT":
        await update.message.reply_text(
            "📍 Местоположение получено. Хотите добавить комментарий к заказу?",
            reply_markup=ReplyKeyboardMarkup([["❌ Без комментария"]], resize_keyboard=True)
        )
    else:
        await handle_contact(update, context)  """  # Начиная от хандл локейшн закомментировано


async def handle_address(update: Update, context: CallbackContext):
    """Обрабатывает адрес (текст или геолокацию)"""
    if context.user_data.get("state") != "AWAITING_ADDRESS":
        await handle_message(
            update, context
        )  # Передаем неподходящие сообщения общему обработчику
        return

    # Обработка геолокации
    if update.message.location:
        location = update.message.location
        context.user_data["pending_order"]["location"] = {
            "latitude": location.latitude,
            "longitude": location.longitude,
        }
        address_msg = "📍 Местоположение принято"

    # Обработка текстового адреса (исключаем служебные команды)
    elif update.message.text and update.message.text != "❌ Без комментария":
        context.user_data["pending_order"]["address"] = update.message.text
        address_msg = f"🏠 Адрес сохранён: {update.message.text}"

    else:
        await update.message.reply_text("Пожалуйста, укажите адрес")
        return

    # Запрос комментария после адреса
    await update.message.reply_text(
        f"{address_msg}\n\n"
        "💬 Добавьте комментарий к заказу (например: «домофон не работает»):",
        reply_markup=ReplyKeyboardMarkup(
            [["❌ Без комментария"]], resize_keyboard=True
        ),
    )
    context.user_data["state"] = "AWAITING_COMMENT"


async def handle_confirmation(update: Update, context: CallbackContext):
    if context.user_data.get("state") != "AWAITING_CONFIRMATION":
        return

    user_id = update.effective_user.id
    user = update.effective_user
    text = update.message.text

    if text == "✅ Подтвердить заказ":
        order_data = context.user_data.get("pending_order")

        try:
            # Создаем заказ в БД
            order_id = await create_order(
                user_id, order_data["total"], order_data.get("comment", "")
            )
            await add_order_items(order_id, order_data["cart_items"])

            # Очищаем корзину
            await clear_cart(user_id)

            # Формируем сообщение для пользователя
            user_message = "✅ Ваш заказ подтвержден!\n\n"
            user_message += f"🆔 Номер заказа: #{order_id}\n"
            user_message += f"💰 Сумма: {order_data['total']} сум\n"
            user_message += f"📱 Телефон: {order_data['phone']}\n"
            user_message += "\n📞 Мы свяжемся с вами в ближайшее время!"

            await update.message.reply_text(
                user_message, reply_markup=main_menu_keyboard()
            )

            # Формируем сообщение для администратора
            admin_message = "🛒 Новый заказ!\n\n"
            admin_message += f"👤 Пользователь: {user.full_name} (ID: {user_id})\n"
            admin_message += f"📱 Телефон: {order_data['phone']}\n"
            admin_message += f"🆔 Номер заказа: #{order_id}\n"
            admin_message += f"💰 Сумма: {order_data['total']} сум\n\n"
            admin_message += "📋 Состав заказа:\n"

            for item in order_data["cart_items"]:
                admin_message += f"• {item[1]} - {item[3]} шт. × {item[2]} сум\n"

            # Добавляем информацию о геолокации, если она есть
            if "location" in order_data:
                admin_message += f"\n📍 Локация: https://maps.google.com/?q={order_data['location']['latitude']},{order_data['location']['longitude']}"
            elif "address" in order_data:
                admin_message += f"\n🏠 Адрес: {order_data['address']}"

            # Отправляем уведомление администратору
            try:
                await context.bot.send_message(chat_id=ADMIN_ID, text=admin_message)
                # Если есть геолокация, отправляем её как карту
                if "location" in order_data:
                    await context.bot.send_location(
                        chat_id=ADMIN_ID,
                        latitude=order_data["location"]["latitude"],
                        longitude=order_data["location"]["longitude"],
                        live_period=86400,  # 24 часа
                    )
            except Exception as e:
                logger.error(f"Ошибка при отправке уведомления администратору: {e}")

        except Exception as e:
            logger.error(f"Ошибка при оформлении заказа: {str(e)}")
            await update.message.reply_text(
                "❌ Произошла ошибка при оформлении заказа. Пожалуйста, попробуйте позже.",
                reply_markup=main_menu_keyboard(),
            )
    else:
        await update.message.reply_text(
            "❌ Заказ отменен.", reply_markup=main_menu_keyboard()
        )

    # Очищаем временные данные
    context.user_data.pop("pending_order", None)
    context.user_data["state"] = STATE_MAIN_MENU


async def clear_cart_handler(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    await clear_cart(user_id)
    await update.message.reply_text(
        "🗑 Корзина очищена", reply_markup=main_menu_keyboard()
    )


async def show_order_history(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    orders = await get_order_history(user_id)

    if not orders:
        await update.message.reply_text(
            "📋 У вас пока нет заказов", reply_markup=main_menu_keyboard()
        )
        return

    message = "📋 История ваших заказов:\n\n"
    for order in orders:
        message += f"🔹 Заказ #{order[0]}\n"
        message += f"📅 {order[1]}\n"
        message += f"💰 Сумма: {order[2]} сум\n"
        message += f"🔄 Статус: {order[3]}\n\n"

    await update.message.reply_text(message, reply_markup=main_menu_keyboard())


async def handle_back(update: Update, context: CallbackContext):
    state = context.user_data.get("state", STATE_MAIN_MENU)

    if state == STATE_CATEGORIES:
        await update.message.reply_text(
            "Главное меню:", reply_markup=main_menu_keyboard()
        )
        context.user_data["state"] = STATE_MAIN_MENU
        return  # ← ДОБАВИТЬ
    elif state == STATE_DISHES:
        await show_categories(update, context)  # ← ЗАМЕНИТЬ НА ЭТО
        return  # ← ДОБАВИТЬ
    elif state == STATE_CART:
        return  # ← ДОБАВИТЬ


async def admin_panel(update: Update, context: CallbackContext):
    logger.info(f"Запрос админ-панели от: {update.effective_user.id}")
    if not is_admin(update.effective_user.id):
        print(f"❌ Отказ: {update.effective_user.id} нет в {ADMIN_IDS}")
        await update.message.reply_text("⛔ Доступ запрещен")
        return

    print(f"✅ Доступ разрешен для: {update.effective_user.id}")
    await update.message.reply_text(
        "⚙️ *Панель администратора*",
        reply_markup=admin_main_keyboard(),
        parse_mode="Markdown",
    )
    context.user_data["mode"] = "admin"


async def admin_stats(update: Update, context: CallbackContext):
    async with aiosqlite.connect(DB_PATH) as db:
        total_orders = await (
            await db.execute("SELECT COUNT(*) FROM orders")
        ).fetchone()
        revenue = await (
            await db.execute("SELECT SUM(total_amount) FROM orders")
        ).fetchone()
        active = await (
            await db.execute("SELECT COUNT(*) FROM orders WHERE status != 'Доставлен'")
        ).fetchone()

    await update.message.reply_text(
        f"📊 *Статистика:*\n\n• Всего заказов: `{total_orders[0]}`\n• Активных: `{active[0]}`\n• Выручка: `{revenue[0] or 0} сум`",
        parse_mode="Markdown",
    )


async def admin_active_orders(update: Update, context: CallbackContext):
    async with aiosqlite.connect(DB_PATH) as db:
        orders = await (
            await db.execute(
                "SELECT id, user_id, total_amount, status FROM orders WHERE status != 'Доставлен'"
            )
        ).fetchall()

    if not orders:
        await update.message.reply_text("📭 Нет активных заказов")
        return

    context.user_data["active_orders"] = orders

    keyboard = []
    for order in orders:
        keyboard.append([f"🛒 Заказ #{order[0]} (Статус: {order[3]})"])
    keyboard.append(["🔙 Назад"])

    await update.message.reply_text(
        "📦 Выберите заказ для изменения статуса:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        parse_mode="Markdown",
    )
    context.user_data["admin_action"] = "order_selection"


async def admin_change_status(update: Update, context: CallbackContext):
    if context.user_data.get("admin_action") != "order_control":
        return

    new_status = update.message.text
    if new_status not in ADMIN_ORDER_STATUSES:
        return

    order_id = context.user_data.get("selected_order_id")
    if not order_id:
        await update.message.reply_text("❌ Не выбран заказ")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE orders SET status = ? WHERE id = ?", (new_status, order_id)
        )
        await db.commit()

        cursor = await db.execute(
            "SELECT user_id FROM orders WHERE id = ?", (order_id,)
        )
        user_row = await cursor.fetchone()
        await cursor.close()

        if user_row:
            user_id = user_row[0]
            status_emoji = {
                "В обработке": "🔄",
                "Готовится": "🧑‍🍳",
                "В пути": "🚚",
                "Доставлен": "✅",
            }.get(new_status, "")

            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"""
🔔 *Статус вашего заказа #{order_id} изменён:*
{status_emoji} *{new_status}*

Спасибо, что выбрали нас! 😊
                    """,
                    parse_mode="Markdown",
                )

                if new_status == "Доставлен":
                    await ask_for_review(context, user_id, order_id)

            except Exception as e:
                logger.error(f"Ошибка отправки уведомления: {e}")

    await update.message.reply_text(
        f"✅ Статус заказа #{order_id} обновлён на: *{new_status}*",
        parse_mode="Markdown",
        reply_markup=admin_main_keyboard(),
    )
    context.user_data.pop("admin_action", None)
    context.user_data.pop("selected_order_id", None)


async def search_order(update: Update, context: CallbackContext):
    """Упрощенный поиск заказа только по ID"""
    query = update.message.text

    if not query.isdigit():
        await update.message.reply_text("❌ Введите только номер заказа (цифры)")
        return

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            # Поиск заказа по ID
            cursor = await db.execute(
                """SELECT o.id, o.status, o.total_amount, o.order_date, 
                   o.comment, u.phone, u.name 
                FROM orders o
                LEFT JOIN users u ON o.user_id = u.id
                WHERE o.id = ?""",
                (int(query),),
            )
            order = await cursor.fetchone()
            await cursor.close()

            if not order:
                await update.message.reply_text("❌ Заказ не найден")
                return

            # Получаем состав заказа
            cursor = await db.execute(
                """SELECT d.name, oi.quantity, oi.price 
                FROM order_items oi 
                JOIN dishes d ON oi.dish_id = d.id 
                WHERE oi.order_id = ?""",
                (order[0],),
            )
            items = await cursor.fetchall()
            await cursor.close()

            # Формируем сообщение
            message = (
                f"🔍 *Заказ #{order[0]}*\n"
                f"👤 Клиент: {order[6] or 'не указан'}\n"
                f"📱 Телефон: {order[5] or 'не указан'}\n"
                f"📅 Дата: `{order[3]}`\n"
                f"🔄 Статус: `{order[1]}`\n"
                f"💰 Сумма: `{order[2]} сум`\n"
                f"📝 Комментарий: `{order[4] or 'нет'}`\n\n"
                "🍽 Состав:\n"
                + "\n".join(
                    f"• {item[0]} — {item[1]} шт. × {item[2]} сум" for item in items
                )
            )
            await update.message.reply_text(message, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Ошибка поиска заказа: {e}", exc_info=True)
        await update.message.reply_text("❌ Произошла ошибка при поиске заказа")
    finally:
        context.user_data.pop("admin_action", None)


async def handle_message(update: Update, context: CallbackContext):
    # Проверяем данные от Mini App
    if hasattr(update.message, "web_app_data") and update.message.web_app_data:
        await handle_mini_app_order(update, context)
        return

    if (
        update.message.text
        and update.message.text.strip().startswith("{")
        and update.message.text.strip().endswith("}")
    ):
        try:
            data = json.loads(update.message.text)
            if data.get("action") == "mini_app_order":
                await handle_mini_app_order(update, context)
                return
        except json.JSONDecodeError:
            # Это не JSON, продолжаем как обычно
            pass
        except Exception as e:
            logger.error(f"Ошибка обработки Mini App JSON: {e}")

    if not hasattr(update, "message") or not update.message:
        logger.error("Получен update без сообщения")
        return

    text = update.message.text
    user_id = update.effective_user.id  # Добавляем получение user_id
    user_data = context.user_data  # Сохраняем user_data
    current_state = user_data.get("state", STATE_MAIN_MENU)

    # Логирование с проверкой
    logger.debug(f"Обработка сообщения от {user_id}: {text}")
    logger.debug(f"Текущее состояние: {current_state}")

    # Режим администратора
    if is_admin(update.effective_user.id) and text == "/admin":
        return

    if user_data.get("mode") == "admin":
        if text == "📊 Статистика":
            await admin_stats(update, context)
        elif text == "📦 Активные заказы":
            await admin_active_orders(update, context)
        elif text == "🔍 Найти заказ":
            await update.message.reply_text(
                "Введите ID заказа:",
                reply_markup=ReplyKeyboardMarkup([["🔙 Назад"]], resize_keyboard=True),
            )
            user_data["admin_action"] = "search_order"
            return
        elif text in ADMIN_ORDER_STATUSES:
            await admin_change_status(update, context)
        elif text == "🔙 Назад":
            if user_data.get("admin_action"):
                user_data.pop("admin_action", None)
            await admin_panel(update, context)
            return
        elif text == "🔙 Выйти из админки":
            await start(update, context)
            user_data["mode"] = None
            return
        elif user_data.get("admin_action") == "order_selection" and text.startswith(
            "🛒 Заказ #"
        ):
            try:
                order_id = int(text.split("#")[1].split()[0])
                user_data["selected_order_id"] = order_id
                user_data["admin_action"] = "order_control"
                await update.message.reply_text(
                    f"Выберите новый статус для заказа #{order_id}:",
                    reply_markup=admin_orders_keyboard(),
                )
                return
            except Exception as e:
                logger.error(f"Ошибка выбора заказа: {e}")
                await update.message.reply_text("❌ Ошибка выбора заказа")
                return
        elif user_data.get("admin_action") == "search_order":
            if text == "🔙 Назад":
                await admin_panel(update, context)
                return
            await search_order(update, context)
            return
        return

    # Обработка для обычных пользователей
    if current_state == "AWAITING_CONFIRMATION":
        await handle_confirmation(update, context)
    elif current_state == "AWAITING_COMMENT":
        if text != "❌ Без комментария":
            context.user_data["pending_order"]["comment"] = text

        phone_keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton("📞 Отправить номер телефона", request_contact=True)]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await update.message.reply_text(
            "Для оформления заказа нам нужен ваш номер телефона.",
            reply_markup=phone_keyboard,
        )
        context.user_data["state"] = "AWAITING_PHONE"
    elif current_state == "AWAITING_REVIEW_COMMENT":
        await save_review(update, context)
    elif text in ["➕ Увеличить", "➖ Уменьшить"]:
        await handle_quantity_change(update, context)
    elif text == "📋 История заказов":
        await show_order_history(update, context)
    elif text == "🍽 Меню":
        await show_categories(update, context)
    elif text == "🛒 Корзина":
        await show_cart(update, context)
    elif text == "🔙 Назад":
        await handle_back(update, context)
    elif text == "🔙 Назад в категории":
        await show_categories(update, context)
    elif text == "🔙 Назад в меню":
        await update.message.reply_text(
            "Главное меню:", reply_markup=main_menu_keyboard()
        )
        context.user_data["state"] = STATE_MAIN_MENU
    elif text == "✅ Добавить в корзину":
        await add_to_cart_handler(update, context)
    elif text == "✅ Оформить заказ":
        await checkout(update, context)
    elif text == "🗑 Очистить корзину":
        await clear_cart_handler(update, context)
    elif current_state == STATE_CATEGORIES:
        await show_dishes(update, context)
    elif current_state == STATE_DISHES:
        await show_dish_details(update, context)
    else:
        await update.message.reply_text(
            "Я не понимаю эту команду. Пожалуйста, используйте кнопки меню.",
            reply_markup=main_menu_keyboard(),
        )
    # Сохраняем user_data обратно в context
    # context.user_data = user_data


# ========== MINI APP ORDER HANDLER ==========
async def handle_mini_app_order(update: Update, context: CallbackContext):
    """Обрабатывает заказ из Mini App"""
    try:
        user = update.effective_user

        # Получаем данные из web_app_data ИЛИ из текста сообщения
        if hasattr(update.message, "web_app_data") and update.message.web_app_data:
            data = json.loads(update.message.web_app_data.data)
        else:
            data = json.loads(update.message.text)

        if data.get("action") == "mini_app_order":
            items = data["items"]
            total = data["total"]

            # Сохраняем заказ во временные данные
            context.user_data["pending_order"] = {
                "cart_items": [
                    (item["id"], item["name"], item["price"], item["quantity"])
                    for item in items
                ],
                "total": total,
                "source": "mini_app",
            }

            # Переходим к сбору адреса
            keyboard = [
                [KeyboardButton("📍 Отправить местоположение", request_location=True)]
            ]

            await update.message.reply_text(
                "📦 *Заказ из Mini App получен!*\n\n"
                f"💰 Сумма: {total} сум\n"
                f"🍽 Позиций: {len(items)}\n\n"
                "Теперь укажите адрес доставки:\n"
                "1. Нажмите кнопку для отправки геолокации\n"
                "2. Или напишите адрес в чат\n\n"
                "⚠️ Для кнопки включите геолокацию на телефоне",
                reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
                parse_mode="Markdown",
            )
            context.user_data["state"] = "AWAITING_ADDRESS"

    except Exception as e:
        logger.error(f"Ошибка обработки заказа из Mini App: {e}")
        await update.message.reply_text(
            "❌ Ошибка обработки заказа. Попробуйте еще раз.",
            reply_markup=main_menu_keyboard(),
        )


# ========== ЗАПУСК И ОСТАНОВКА ==========
async def shutdown(application):
    try:
        if hasattr(application, "updater") and application.updater.running:
            await application.updater.stop()
        await application.stop()
        await application.shutdown()
        logger.info("Бот успешно остановлен")
    except Exception as e:
        logger.error(f"Ошибка при остановке: {e}")


async def run_bot():
    application = None
    try:
        application = (
            Application.builder()
            .token(TOKEN)
            .read_timeout(30)
            .write_timeout(30)
            .pool_timeout(30)
            .build()
        )
        # Обработчики команд
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("admin", admin_panel))

        # Обработчик callback-запросов (для inline-кнопок)
        application.add_handler(
            CallbackQueryHandler(handle_review_callback, pattern="^rate_")
        )

        # Обработчик контактов
        application.add_handler(MessageHandler(filters.CONTACT, handle_contact))

        application.add_handler(
            MessageHandler(
                filters.TEXT & filters.Regex(r'^{"action":"mini_app_order"'),
                handle_mini_app_order,
            )
        )

        # Обработчик адресов (объединенная версия)
        application.add_handler(
            MessageHandler(
                filters.LOCATION
                | (
                    filters.TEXT
                    & ~filters.COMMAND
                    & ~filters.Regex(r"^(🏠 Ввести адрес вручную|❌ Без комментария)$")
                ),
                handle_address,
            )
        )

        # Общий текстовый обработчик (должен быть ПОСЛЕДНИМ)
        application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
        )

        await application.initialize()
        await application.start()
        await application.updater.start_polling(drop_pending_updates=True, timeout=30)

        logger.info("Бот успешно запущен")

        while True:
            await asyncio.sleep(1)

    except httpx.NetworkError as e:
        logger.error(f"Сетевая ошибка: {e}")
    except NetworkError as e:
        logger.error(f"Ошибка сети Telegram: {e}")
    except Exception as e:
        logger.error(f"Неожиданная ошибка: {e}")
    finally:
        if application:
            logger.info("Завершение работы бота...")
            try:
                await shutdown(application)
            except Exception as e:
                logger.error(f"Финальная ошибка: {e}")


def main():
    if not DB_PATH.exists():
        init_db()

    # Запускаем веб-сервер для Mini App
    start_web_server_thread()

    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"Фатальная ошибка: {e}")


if __name__ == "__main__":
    main()
