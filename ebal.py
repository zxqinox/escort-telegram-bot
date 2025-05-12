import os
import logging
import sqlite3
from datetime import datetime
from typing import Dict, Tuple, List, Optional
from dotenv import load_dotenv
from cachetools import TTLCache

load_dotenv()

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InputTextMessageContent,
    InlineQueryResultArticle,
    Location,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
    ContextTypes,
    filters,
    ConversationHandler,
)
from telegram.constants import ParseMode
from geopy.geocoders import Nominatim
from apscheduler.schedulers.background import BackgroundScheduler
from geopy.geocoders import GoogleV3

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("geopy").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
geocoder_cache = TTLCache(maxsize=2000, ttl=3600)  # Увеличен размер кэша

# Конфигурация
TOKEN = os.getenv("TELEGRAM_TOKEN")
DB_NAME = "bot_catalog.db"
ADMIN_ID = int(os.getenv("ADMIN_ID"))

# Состояния
(
    SELECT_CITY,
    MANUAL_CITY_INPUT,
    CONFIRM_CITY,
    MAIN_MENU,
    DEPOSIT_AMOUNT,
    GET_MODEL_DATA,
    GET_MODEL_PHOTO,
    CONFIRM_DELETE_MODEL 
) = range(8)

geolocator = Nominatim(
    user_agent="rakalbalenci@outlook.com",  # Используйте реальный email
    timeout=15
)
google_geocoder = GoogleV3(
    api_key=os.getenv("GOOGLE_API_KEY"),  # Добавьте ключ в .env
    domain="maps.googleapis.com",
    timeout=15
)

class Database:
    def __init__(self, db_name: str):
        self.conn = sqlite3.connect(db_name)
        self.cursor = self.conn.cursor()
        self._initialize_db()

    def _initialize_db(self):
        """Инициализация таблиц в базе данных"""
        self.cursor.execute('''CREATE TABLE IF NOT EXISTS users
                            (user_id INTEGER PRIMARY KEY, 
                             city TEXT, 
                             balance INTEGER DEFAULT 0)''')

        self.cursor.execute('''CREATE TABLE IF NOT EXISTS models
                            (id INTEGER PRIMARY KEY AUTOINCREMENT,
                            name TEXT, 
                            age INTEGER, 
                            city TEXT,
                            photos TEXT, 
                            price INTEGER)''')

        self.cursor.execute('''CREATE TABLE IF NOT EXISTS orders
                            (id INTEGER PRIMARY KEY AUTOINCREMENT,
                            user_id INTEGER, 
                            model_id INTEGER,
                            hours INTEGER, 
                            services TEXT,
                            total INTEGER, 
                            status TEXT)''')
        
        self.cursor.execute('''CREATE INDEX IF NOT EXISTS idx_models_city 
                            ON models(city)''')
        self.cursor.execute('''CREATE INDEX IF NOT EXISTS idx_orders_user 
                            ON orders(user_id)''')
        self.conn.commit()

    def execute(self, query: str, params: tuple = ()) -> None:
        try:
            self.cursor.execute(query, params)
            self.conn.commit()
        except sqlite3.Error as e:
            logging.error(f"Database error: {e}")
            self.conn.rollback()
        
    def fetch_one(self, query: str, params: tuple = ()) -> Optional[Dict]:
        self.cursor.execute(query, params)
        columns = [col[0] for col in self.cursor.description]
        row = self.cursor.fetchone()
        return dict(zip(columns, row)) if row else None
        
    def fetch_all(self, query: str, params: tuple = ()) -> List[Dict]:
        self.cursor.execute(query, params)
        columns = [col[0] for col in self.cursor.description]
        return [dict(zip(columns, row)) for row in self.cursor.fetchall()]

# Инициализация базы данных
db = Database(DB_NAME)

def check_geocoder():
    try:
        test_location = geolocator.reverse((55.7558, 37.6176), exactly_one=True)
        logging.info("Geocoder connection OK")
    except Exception as e:
        logging.critical(f"Geocoder connection failed: {e}")

async def get_city_from_location(location: Location) -> str:
    cache_key = f"{location.latitude}_{location.longitude}"
    
    if cache_key in geocoder_cache:
        logging.info(f"Using cached city for {cache_key}")
        return geocoder_cache[cache_key]
    
    try:
        # Попытка через Nominatim
        geo_location = geolocator.reverse(
            (location.latitude, location.longitude),
            exactly_one=True,
            language="ru",
            addressdetails=True
        )
        
        if geo_location:
            address = geo_location.raw.get('address', {})
            city = (
                address.get('city') 
                or address.get('town') 
                or address.get('village')
                or address.get('county')
                or address.get('state')
                or 'Unknown'
            )
            
            if city != 'Unknown':
                geocoder_cache[cache_key] = city
                logging.info(f"Nominatim success: {city}")
                return city

        # Если Nominatim не справился, пробуем Google
        logging.warning("Falling back to Google Geocoder")
        google_location = google_geocoder.reverse(
            (location.latitude, location.longitude),
            exactly_one=True,
            language="ru"
        )
        
        if google_location:
            address = google_location.raw.get('address_components', [])
            city = next(
                (comp['long_name'] for comp in address 
                 if 'locality' in comp['types'] or 
                    'administrative_area_level_2' in comp['types']),
                'Unknown'
            )
            
            if city != 'Unknown':
                geocoder_cache[cache_key] = city
                logging.info(f"Google Geocoder success: {city}")
                return city

        logging.error("All geocoders failed")
        return 'Unknown'

    except Exception as e:
        logging.error(f"Geocoding critical error: {str(e)}")
        return 'Unknown'

# Добавьте проверку работы геокодеров при старте
def check_geocoders():
    test_coords = (55.7558, 37.6176)  # Координаты Москвы
    
    try:
        # Проверка Nominatim
        nom_result = geolocator.reverse(test_coords, language="ru")
        logging.info(f"Nominatim test: {nom_result.address[:60]}...")
        
        # Проверка Google
        if os.getenv("GOOGLE_API_KEY"):
            google_result = google_geocoder.reverse(test_coords, language="ru")
            logging.info(f"Google test: {google_result.address[:60]}...")
            
    except Exception as e:
        logging.critical(f"Geocoder test failed: {e}")

async def send_photo(context: ContextTypes.DEFAULT_TYPE, chat_id: int, 
                   file_id: str, caption: str, reply_markup=None):
    try:
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=file_id,
            caption=caption,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
    except Exception as e:
        logging.error(f"Error sending photo: {e}")

# Обработчики команд
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.execute(
        "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
        (user_id,)
    )
    
    keyboard = [[InlineKeyboardButton("Продолжить", callback_data='continue')]]
    
    await send_photo(
        context,
        update.effective_chat.id,
        "welcome.jpg",
        "Добро пожаловать!\n\nПользуясь нашим бот-каталогом...",
        InlineKeyboardMarkup(keyboard)
    )

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info(f"Получена геопозиция: {update.message.location}")
    try:
        city = await get_city_from_location(update.message.location)
        logging.info(f"Определен город: {city}")  # Добавлено
        if city == 'Unknown':
            await update.message.reply_text("❌ Не удалось определить город. Попробуйте ввести вручную.")
            return MANUAL_CITY_INPUT
        return await validate_and_confirm_city(update, context, city)
    except Exception as e:
        logging.error(f"Ошибка обработки геопозиции: {e}")
        await update.message.reply_text("❌ Ошибка определения города. Попробуйте ещё раз или введите вручную.")
        return MANUAL_CITY_INPUT

async def ask_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [KeyboardButton("📍 Отправить геопозицию", request_location=True)],
        [KeyboardButton("🏙 Ввести город вручную")]
    ]
    
    await update.callback_query.message.reply_text(
        "Разрешите доступ к геопозиции или введите город:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )
    return SELECT_CITY

async def handle_manual_city_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "🏙 Ввести город вручную":
        await update.message.reply_text("Введите город:")
        return MANUAL_CITY_INPUT
    # Добавьте обработку текста города
    city = update.message.text
    return await validate_and_confirm_city(update, context, city)

async def validate_city(city: str) -> bool:
    try:
        location = geolocator.geocode(
            city, 
            exactly_one=True, 
            language="ru",
            timeout=15
        )
        if location:
            logging.info(f"Validation success for: {city}")
            return True
        logging.warning(f"City not found: {city}")
        return False
    except Exception as e:
        logging.error(f"City validation crashed: {e}")
        return False

async def validate_and_confirm_city(update: Update, context: ContextTypes.DEFAULT_TYPE, city: str):
    # Сохраняем город в базу данных
    user_id = update.effective_user.id
    db.execute(
        "UPDATE users SET city = ? WHERE user_id = ?",
        (city.lower(), user_id)
    )
    
    if not await validate_city(city):
        await update.message.reply_text("Не удалось найти такой город. Попробуйте ещё раз:")
        return MANUAL_CITY_INPUT

    context.user_data['city'] = city.lower()
    keyboard = [
        [InlineKeyboardButton(f"✅ Да, город {city}", callback_data='confirm_city')],
        [InlineKeyboardButton("🔄 Изменить город", callback_data='change_city')]
    ]

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"Подтвердите выбор города: {city.capitalize()}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CONFIRM_CITY

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'wait_message_id' in context.user_data:
        await context.bot.delete_message(
            chat_id=update.effective_chat.id,
            message_id=context.user_data['wait_message_id']
        )
    
    keyboard = [
        [InlineKeyboardButton("Поиск моделей", switch_inline_query_current_chat="")],
        [InlineKeyboardButton("Мой аккаунт", callback_data='my_account')]
    ]
    
    await send_photo(
        context,
        update.effective_chat.id,
        "main_menu.jpg",
        "Главное меню",
        InlineKeyboardMarkup(keyboard)
    )
    return MAIN_MENU

async def handle_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.inline_query.from_user.id
        user_data = db.fetch_one("SELECT city FROM users WHERE user_id = ?", (user_id,))
        
        if not user_data or not user_data.get('city'):
            await update.inline_query.answer([])
            return

        city = user_data['city'].lower()
        query = update.inline_query.query
        offset = int(update.inline_query.offset or 0)
        page_size = 5

        models = db.fetch_all(
            """SELECT * FROM models 
            WHERE LOWER(city) = ? 
            LIMIT ? OFFSET ?""",
            (city, page_size, offset)
        )
        
        results = []
        next_offset = offset + page_size
        
        for model in models:
            results.append(
                InlineQueryResultArticle(
                    id=str(model['id']),
                    title=model['name'],
                    input_message_content=InputTextMessageContent(
                        f"{model['name']} · {model['age']} · {model['city']}"
                    ),
                    description=f"Стоимость: {model['price']}₽",
                    thumb_url=model['photos'],
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Следующая страница", 
                            switch_inline_query_current_chat=f"page_{next_offset}")]
                    ]) if len(models) == page_size else None
                )
            )

        await update.inline_query.answer(
            results, 
            next_offset=str(next_offset) if results else None
        )
    except Exception as e:
        logging.error(f"Inline query error: {e}")
        results = [InlineQueryResultArticle(
            id="error",
            title="Ошибка поиска",
            input_message_content=InputTextMessageContent(
                "⚠️ Произошла ошибка при поиске. Попробуйте позже."
            )
        )]
        await update.inline_query.answer(results)

async def handle_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("По номеру карты", callback_data='deposit_card')],
        [InlineKeyboardButton("Вернуться в мой аккаунт", callback_data='back')]
    ]
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Выберите способ пополнения:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return DEPOSIT_AMOUNT

async def handle_deposit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.replace(',', '.'))
        if not (100 <= amount <= 100000):
            raise ValueError

        amount_kopecks = int(amount * 100)
        user_id = update.effective_user.id
        
        db.execute(
            '''UPDATE users SET balance = balance + ? 
            WHERE user_id = ?''',
            (amount_kopecks, user_id)
        )

        await update.message.reply_text(
            f"✅ Баланс успешно пополнен на {amount:.2f}₽\n"
            f"Новый баланс: {await get_user_balance(user_id):.2f}₽"
        )

    except ValueError:
        await update.message.reply_text("❌ Некорректная сумма. Введите число от 100 до 100 000:")
        return DEPOSIT_AMOUNT

async def get_user_balance(user_id: int) -> float:
    user = db.fetch_one(
        "SELECT balance FROM users WHERE user_id = ?", 
        (user_id,)
    )
    return user['balance'] / 100 if user and user.get('balance') else 0.0

# Админ-панель
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 Доступ запрещён!")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton("➕ Добавить модель", callback_data='add_model'),
         InlineKeyboardButton("🗑 Удалить модель", callback_data='delete_model')],
        [InlineKeyboardButton("📊 Статистика", callback_data='stats'),
         InlineKeyboardButton("📦 Резервная копия", callback_data='backup')]
    ]

    await update.message.reply_text(
        "🛠 Админ-панель:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def add_model_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.message.reply_text(
        "Введите данные модели в формате:\n"
        "Имя | Возраст | Город | Цена\n"
        "Пример: Анна | 25 | Москва | 5000"
    )
    return GET_MODEL_DATA

async def save_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        parts = list(map(str.strip, update.message.text.split('|')))
        if len(parts) != 4:
            raise ValueError
        
        name, age_str, city, price_str = parts
        age = int(age_str)
        price = int(price_str)
        
        if not name or age < 18 or price <= 0:
            raise ValueError
            
        context.user_data['new_model'] = {
            'name': name,
            'age': age,
            'city': city,
            'price': price
        }
        
        await update.message.reply_text("Теперь отправьте фото модели")
        return GET_MODEL_PHOTO
        
    except Exception as e:
        logging.error(f"Model save error: {e}")
        await update.message.reply_text("Ошибка формата! Попробуйте снова")
        return GET_MODEL_DATA

async def save_model_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        photo = update.message.photo[-1]
        file_id = photo.file_id
        model = context.user_data['new_model']
        
        db.execute('''INSERT INTO models 
                   (name, age, city, photos, price)
                   VALUES (?, ?, ?, ?, ?)''',
                   (model['name'], model['age'], 
                    model['city'], file_id, model['price']))
        
        await update.message.reply_text("Модель успешно добавлена!")
    except Exception as e:
        logging.error(f"Photo save error: {e}")
        await update.message.reply_text("Ошибка сохранения модели!")
    finally:
        context.user_data.pop('new_model', None)
        return ConversationHandler.END

async def delete_model_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    models = db.fetch_all("SELECT id, name FROM models LIMIT 50")
    
    keyboard = [
        [InlineKeyboardButton(f"{m['id']}: {m['name']}", callback_data=f"del_{m['id']}")]
        for m in models
    ]
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='back_admin')])

    await update.callback_query.message.edit_text(
        "Выберите модель для удаления:",
        reply_markup=InlineKeyboardMarkup(keyboard))
    return CONFIRM_DELETE_MODEL

async def confirm_delete_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    model_id = int(update.callback_query.data.split('_')[1])
    model = db.fetch_one("SELECT * FROM models WHERE id = ?", (model_id,))
    
    context.user_data['pending_delete'] = model_id
    
    keyboard = [
        [InlineKeyboardButton("✅ Подтвердить удаление", callback_data='confirm_del')],
        [InlineKeyboardButton("🔙 Отмена", callback_data='cancel_del')]
    ]

    await update.callback_query.message.edit_text(
        f"Вы уверены, что хотите удалить модель?\n"
        f"ID: {model['id']}\nИмя: {model['name']}\nГород: {model['city']}",
        reply_markup=InlineKeyboardMarkup(keyboard))
    return CONFIRM_DELETE_MODEL

# Резервное копирование
def backup_db():
    try:
        backup_dir = os.path.abspath(os.getenv("BACKUP_DIR", "backups"))
        os.makedirs(backup_dir, exist_ok=True)
        
        backup_name = f"backup_{datetime.now().strftime('%Y%m%d_%H%M')}.db"
        backup_path = os.path.join(backup_dir, backup_name)
        
        with open(backup_path, 'wb') as f:
            for line in db.conn.iterdump():
                f.write(f'{line}\n'.encode('utf-8'))
        
        logging.info(f"Backup created: {backup_path}")
    except Exception as e:
        logging.error(f"Backup failed: {e}")

scheduler = BackgroundScheduler()
scheduler.add_job(backup_db, 'interval', hours=24)
scheduler.start()

async def handle_callback_queries(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        await query.answer()
    except Exception as e:
        logging.error(f"Callback error: {e}")
        return

    data = query.data
    user_data = context.user_data
    
    if data == 'continue':
        return await ask_city(update, context)
    
    elif data == 'my_account':
        return await show_account_menu(update, context)
    
    elif data == 'deposit_card':
        await query.edit_message_text("Введите сумму для пополнения (от 100 до 100 000 ₽):")
        return DEPOSIT_AMOUNT
    
    elif data == 'back':
        return await show_main_menu(update, context)
    
    elif data == 'add_model':
        return await add_model_flow(update, context)
    
    elif data.startswith('del_'):
        return await confirm_delete_model(update, context)
    
    elif data == 'confirm_del':
        model_id = user_data.get('pending_delete')
        db.execute("DELETE FROM models WHERE id = ?", (model_id,))
        await query.edit_message_text("Модель успешно удалена!")
        return await admin_panel(update, context)
    
    elif data == 'cancel_del':
        return await delete_model_flow(update, context)
    
    elif data == 'back_admin':
        return await admin_panel(update, context)
        
    elif data == 'auto_city':
        await query.message.reply_text("Отправьте ваше местоположение:")
        return SELECT_CITY
    
    elif data == 'manual_city':
        await query.message.reply_text("Введите город вручную:")
        return MANUAL_CITY_INPUT

async def show_account_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    balance = await get_user_balance(user_id)
    
    keyboard = [
        [InlineKeyboardButton("💰 Пополнить баланс", callback_data='deposit_card')],
        [InlineKeyboardButton("📖 История заказов", callback_data='orders_history')],
        [InlineKeyboardButton("🔙 Назад", callback_data='back')]
    ]
    
    await update.callback_query.edit_message_caption(
        caption=f"Ваш баланс: {balance:.2f}₽\nВыберите действие:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return MAIN_MENU

def main():
    check_geocoders()
    application = Application.builder().token(TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            SELECT_CITY: [
                MessageHandler(filters.LOCATION, handle_location),
                MessageHandler(filters.TEXT & filters.Regex(r"^🏙 Ввести город вручную$"), ask_city)
            ],
            MANUAL_CITY_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_manual_city_input)
            ],
            CONFIRM_CITY: [
                CallbackQueryHandler(handle_callback_queries, pattern='^(confirm_city|change_city)$')
            ],
            MAIN_MENU: [
                CallbackQueryHandler(handle_callback_queries),
                InlineQueryHandler(handle_inline_query)
            ],
            DEPOSIT_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_deposit_amount)
            ],
            GET_MODEL_DATA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_model)
            ],
            GET_MODEL_PHOTO: [
                MessageHandler(filters.PHOTO, save_model_photo)
            ],
            CONFIRM_DELETE_MODEL: [
                CallbackQueryHandler(handle_callback_queries)
            ]
        },
        fallbacks=[CommandHandler('admin', admin_panel)],
        allow_reentry=True
    )
    
    # Регистрация обработчиков
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler('admin', admin_panel))
    application.add_handler(CallbackQueryHandler(handle_callback_queries))
    
    try:
        application.run_polling()
    finally:
        db.conn.close()
        scheduler.shutdown()

if __name__ == '__main__':
    main()