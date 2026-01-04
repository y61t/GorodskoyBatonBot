import os
import json
import time
import re
import logging
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Dict
from fastapi import FastAPI
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, PreCheckoutQuery
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

# === Настройки и логирование ===
load_dotenv()
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
PROVIDER_TOKEN = "390540012:LIVE:81586"
CURRENCY = os.getenv("CURRENCY", "RUB")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
SHEET_ID = os.getenv("SHEET_ID")

if not BOT_TOKEN or not PROVIDER_TOKEN:
    raise ValueError("BOT_TOKEN или PROVIDER_TOKEN не найдены в .env!")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    logging.info("Бот стартует через FastAPI + long-polling")
    await start_parsing()
    asyncio.create_task(dp.start_polling(bot))

    yield

    # shutdown
    await bot.session.close()
    logging.info("Бот остановлен")

# === FastAPI ===
app = FastAPI(lifespan=lifespan)

# === Бот и Dispatcher ===
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)


# === FSM ===
class OrderStates(StatesGroup):
    choosing_delivery = State()
    entering_phone = State()
    entering_email = State()
    entering_address = State()
    confirming = State()
    entering_quantity = State()


# === Настройки доставки ===
DELIVERY_OPTIONS = {
    "inside_mkad": {"name": "Внутри МКАД", "price": 45000},
    "outside_mkad": {"name": "За МКАД (до 10 км)", "price": 75000},
    "pickup": {"name": "Забрать с производства", "price": 0}
}

# === Google Sheets ===
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
SERVICE_ACCOUNT_FILE = 'credentials.json'


def get_sheets_service():
    try:
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        return build('sheets', 'v4', credentials=creds)
    except Exception as e:
        logging.error(f"Google Sheets ошибка: {e}")
        return None


# === Кэш и парсинг сайта ===
CACHE_FILE = "catalog.json"
CACHE_DURATION = 3600
CATALOG = {}


def load_catalog():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if time.time() - data.get("timestamp", 0) < CACHE_DURATION:
                return data["catalog"]
    return None


def save_catalog(catalog):
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump({"catalog": catalog, "timestamp": time.time()}, f, ensure_ascii=False, indent=2)


def parse_catalog() -> Dict:
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1920,1080")

    driver = webdriver.Chrome(options=chrome_options)
    catalog = {'Белый хлеб': [], 'Серый хлеб': [], 'Хлеб с добавками': []}
    product_id = 0

    try:
        driver.get("https://gorodskoybaton.ru/")
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(3)

        tabs = {
            'Белый хлеб': 'Белый хлеб',
            'Серый хлеб': 'Серый хлеб',
            'Хлеб с добавками': 'Хлеб с добавками'
        }

        for category, tab_text in tabs.items():
            try:
                button = driver.find_element(By.XPATH, f"//button[contains(text(), '{tab_text}')]")
                driver.execute_script("arguments[0].click();", button)
                time.sleep(3)

                products = driver.find_elements(By.CSS_SELECTOR, ".js-product")
                for prod in products:
                    try:
                        name = prod.find_element(By.CSS_SELECTOR, ".js-product-name").text.strip()
                        if not name or any(skip in name.upper() for skip in [
                            'СТАЖИРОВКА', 'КУРС', 'ТОРТ', 'ПОДАРОК', 'СЕРТИФИКАТ', 'НАБОР'
                        ]):
                            continue
                        if "кекс" in name.lower():
                            continue

                        description = ""
                        try:
                            description = prod.find_element(By.CSS_SELECTOR, ".js-store-prod-descr").text.strip()
                        except:
                            pass

                        weights = []
                        prices = {}
                        try:
                            inputs = prod.find_elements(By.CSS_SELECTOR, "input[name='Вес']")
                            for inp in inputs:
                                val = inp.get_attribute("value")
                                if val and val.isdigit():
                                    weight = f"{val}г"
                                    weights.append(weight)
                                    label = inp.find_element(By.XPATH, "./following-sibling::div")
                                    driver.execute_script("arguments[0].click();", label)
                                    time.sleep(0.4)
                                    price_text = prod.find_element(By.CSS_SELECTOR, ".js-product-price").text
                                    price = int(re.search(r"\d+", price_text.replace(" ", "")).group()) * 100
                                    prices[weight] = price
                        except:
                            pass

                        if not weights:
                            weights = ["350г"]
                            price_text = prod.find_element(By.CSS_SELECTOR, ".js-product-price").text
                            prices["350г"] = int(re.search(r"\d+", price_text.replace(" ", "")).group()) * 100

                        image_url = "https://via.placeholder.com/300x300.png?text=Хлеб"
                        try:
                            img = prod.find_element(By.CSS_SELECTOR, "img.js-product-img")
                            src = img.get_attribute("data-original") or img.get_attribute("src")
                            if src.startswith("//"):
                                src = "https:" + src
                            image_url = src
                        except:
                            pass

                        product_id += 1
                        catalog[category].append({
                            "id": product_id,
                            "name": name,
                            "weights": weights,
                            "prices": prices,
                            "composition": description or "Состав не указан",
                            "image_url": image_url
                        })
                    except:
                        continue


            except Exception as e:
                logging.warning(f"Ошибка категории {category}: {e}")

        return {k: v for k, v in catalog.items() if v}

    except Exception as e:
        logging.error(f"Парсинг не удался: {e}")
        return {}
    finally:
        driver.quit()


async def start_parsing():
    global CATALOG
    cached = load_catalog()
    if cached:
        CATALOG = cached
        logging.info("Каталог загружен из кэша")
    else:
        logging.info("Парсим сайт...")
        CATALOG = parse_catalog()
        if CATALOG:
            save_catalog(CATALOG)


def get_product_by_id(product_id: int):
    for cat in CATALOG.values():
        for item in cat:
            if item.get('id') == product_id:
                return item
    return None


# === Клавиатуры ===
def get_main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🍞 Белый хлеб", callback_data="cat_Белый хлеб")],
        [InlineKeyboardButton(text="🌾 Серый хлеб", callback_data="cat_Серый хлеб")],
        [InlineKeyboardButton(text="🥖 Хлеб с добавками", callback_data="cat_Хлеб с добавками")],
        [InlineKeyboardButton(text="🛒 Корзина", callback_data="cart_view")]
    ])


def get_delivery_keyboard():
    keyboard = []
    for key, opt in DELIVERY_OPTIONS.items():
        price = opt["price"] // 100
        emoji = "🚚" if price > 0 else "🏭"
        text = f"{emoji} {opt['name']} — {price}₽" if price > 0 else f"{emoji} {opt['name']} — бесплатно"
        keyboard.append([InlineKeyboardButton(text=text, callback_data=f"delivery_{key}")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


@dp.message(F.chat.type == "private", StateFilter(None))
async def handle_start(message: types.Message):
    welcome = (
        "👋 *Добро пожаловать в «Городской Батон»!* 🎉\n\n"
        "Свежий хлеб — прямо с производства. Выбирайте категорию ниже 👇\n\n"
        "Как работает бот:\n"
        "- Выберите категорию хлеба из меню ниже.\n"
        "- Просмотрите товары, выберите вес (если доступно) и укажите количество.\n"
        "- Добавьте в корзину и перейдите к оформлению заказа.\n"
        "- Выберите доставку, введите контакты и оплатите."
    )
    await message.answer(welcome, reply_markup=get_main_menu(), parse_mode="Markdown")


@dp.message(F.chat.type == "private", Command("start"))
async def cmd_start(message: types.Message):
    await handle_start(message)


@dp.callback_query(F.message.chat.type == "private", F.data.startswith("cat_"))
async def show_category(callback: types.CallbackQuery):
    cat = callback.data.split("_", 1)[1]
    if cat not in CATALOG or not CATALOG[cat]:
        await callback.message.delete()
        await bot.send_message(
            callback.message.chat.id,
            "😔 Пока нет товаров в этой категории.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
            ])
        )
        return

    keyboard = []
    for item in CATALOG[cat]:
        price = item['prices'].get(item['weights'][0], 0) / 100
        keyboard.append(
            [InlineKeyboardButton(text=f"{item['name']} — 💰 {price:.0f}₽", callback_data=f"item_{item['id']}")]
        )
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")])

    await callback.message.delete()
    await bot.send_message(callback.message.chat.id, f"📦 *{cat}*",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
                           parse_mode="Markdown")


@dp.callback_query(F.message.chat.type == "private", F.data.startswith("item_"))
async def show_item(callback: types.CallbackQuery, state: FSMContext):
    product_id = int(callback.data.split("_", 1)[1])
    item = get_product_by_id(product_id)
    if not item:
        return

    weights = item['weights']
    current_cat = next(cat for cat in CATALOG if any(i['id'] == product_id for i in CATALOG[cat]))
    await state.update_data(current_cat=current_cat)

    if len(weights) == 1:
        weight = weights[0]
        await state.update_data(selected_item={"product_id": product_id, "weight": weight})
        await state.set_state(OrderStates.entering_quantity)

        caption = f"🍞 *{item['name']}*\n\n📋 {item['composition']}\n\nВведите количество (целое число):"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data=f"cat_{current_cat}")]
        ])

        await callback.message.delete()
        img_url = item['image_url']
        if not (img_url.startswith("http") and any(
                img_url.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif"])):
            img_url = "https://via.placeholder.com/300x300.png?text=Хлеб"

        try:
            await bot.send_photo(callback.message.chat.id, img_url, caption=caption, reply_markup=keyboard,
                                 parse_mode="Markdown")
        except:
            await bot.send_message(callback.message.chat.id, caption, reply_markup=keyboard, parse_mode="Markdown")
    else:
        keyboard = []
        for w in weights:
            keyboard.append([
                InlineKeyboardButton(
                    text=w,
                    callback_data=f"add_{product_id}_{w}"
                )
            ])
        keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"cat_{current_cat}")])
        caption = f"🍞 *{item['name']}*\n\n📋 {item['composition']}"

        await callback.message.delete()
        img_url = item['image_url']
        if not (img_url.startswith("http") and any(
                img_url.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif"])):
            img_url = "https://via.placeholder.com/300x300.png?text=Хлеб"

        try:
            await bot.send_photo(callback.message.chat.id, img_url, caption=caption,
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")
        except:
            await bot.send_message(callback.message.chat.id, caption,
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
                                   parse_mode="Markdown")


@dp.callback_query(F.message.chat.type == "private", F.data.startswith("add_"))
async def ask_quantity(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split("_", 2)[1:]
    product_id = int(parts[0])
    weight = parts[1]
    item = get_product_by_id(product_id)
    if not item:
        return

    await state.update_data(selected_item={"product_id": product_id, "weight": weight})
    await state.set_state(OrderStates.entering_quantity)

    current_cat = (await state.get_data()).get("current_cat", "")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"item_{product_id}")]
    ])

    await bot.send_message(
        callback.message.chat.id,
        f"📦 *{item['name']}* ({weight})\n\nВведите количество (целое число):",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


@dp.message(F.chat.type == "private", StateFilter(OrderStates.entering_quantity))
async def add_to_cart_with_quantity(message: types.Message, state: FSMContext):
    quantity_text = message.text.strip()
    if not quantity_text.isdigit() or int(quantity_text) <= 0:
        await message.answer("Введите корректное положительное целое число (например, 2).")
        return

    quantity = int(quantity_text)
    data = await state.get_data()
    item_data = data.get("selected_item")

    if not item_data:
        await message.answer("Ошибка: товар не выбран. Попробуйте снова.")
        await state.set_state(None)
        return

    product_id = item_data["product_id"]
    weight = item_data["weight"]
    item = get_product_by_id(product_id)
    if not item:
        await message.answer("Ошибка: товар не найден.")
        await state.set_state(None)
        return

    price = item['prices'].get(weight, 0)
    total_price = price * quantity

    cart = data.get("cart") or []
    cart.append((product_id, weight, price, quantity))
    await state.update_data(cart=cart)

    await state.update_data(selected_item=None)
    await state.set_state(None)

    await message.answer(
        f"Добавлено: *{item['name']}* ({weight}) × {quantity} — {total_price / 100:.0f}₽",
        parse_mode="Markdown"
    )

    await bot.send_message(message.chat.id, "Выберите категорию:", reply_markup=get_main_menu())


@dp.callback_query(F.message.chat.type == "private", F.data == "cart_view")
async def view_cart(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    cart = data.get("cart") or []
    if not cart:
        await callback.message.delete()
        await bot.send_message(callback.message.chat.id, "🛒 Ваша корзина пуста.",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                   [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
                               ]))
        return

    total = sum(price * qty for _, _, price, qty in cart)
    text = "🛒 *Ваша корзина:*\n\n"
    for product_id, weight, price, qty in cart:
        item = get_product_by_id(product_id)
        if item:
            text += f"• {item['name']} ({weight}) × {qty} — 💰 {(price * qty) / 100:.0f}₽\n"
    text += f"\n💵 *Итого:* {total / 100:.0f}₽"
    keyboard = [
        [InlineKeyboardButton(text="✅ Оформить заказ", callback_data="start_order")],
        [InlineKeyboardButton(text="🗑 Очистить корзину", callback_data="clear_cart")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ]

    await callback.message.delete()
    await bot.send_message(callback.message.chat.id, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
                           parse_mode="Markdown")


@dp.callback_query(F.message.chat.type == "private", F.data == "clear_cart")
async def clear_cart(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(cart=[])
    await callback.message.delete()
    await bot.send_message(callback.message.chat.id, "🛒 Корзина очищена.", reply_markup=get_main_menu())


@dp.callback_query(F.message.chat.type == "private", F.data == "start_order")
async def start_order(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    cart = data.get("cart") or []
    if not cart:
        await callback.answer("🛒 Корзина пуста!", show_alert=True)
        return

    # сохраняем order_total с учётом количества
    order_total = sum(price * qty for _, _, price, qty in cart)
    await state.update_data(order_total=order_total, cart=cart)
    await state.set_state(OrderStates.choosing_delivery)

    await callback.message.delete()
    await bot.send_message(callback.message.chat.id, "🚚 *Выберите способ доставки:*",
                           reply_markup=get_delivery_keyboard(),
                           parse_mode="Markdown")


@dp.callback_query(F.message.chat.type == "private", F.data.startswith("delivery_"))
async def choose_delivery(callback: types.CallbackQuery, state: FSMContext):
    delivery_key = callback.data.split("_", 1)[1]
    delivery = DELIVERY_OPTIONS[delivery_key]
    data = await state.get_data()
    total = data.get("order_total", 0)
    delivery_price = delivery["price"]
    final_total = total + delivery_price

    await state.update_data(
        delivery_option=delivery["name"],
        delivery_price=delivery_price,
        final_total=final_total,
        delivery_key=delivery_key  # Сохраняем ключ для проверки pickup
    )
    await state.set_state(OrderStates.entering_phone)

    await callback.message.delete()
    await bot.send_message(callback.message.chat.id,
                           f"🚚 *Доставка:* {delivery['name']}\n💰 Цена: {'бесплатно' if delivery_price == 0 else f'{delivery_price / 100:.0f}₽'}\n\n☎️ Введите номер телефона:",
                           parse_mode="Markdown")


@dp.message(F.chat.type == "private", StateFilter(OrderStates.entering_phone))
async def enter_phone(message: types.Message, state: FSMContext):
    phone = message.text.strip()
    if not re.match(r"^\+?\d{10,15}$", phone):
        await message.answer("❌ Неверный формат. Пример: +79991234567")
        return
    await state.update_data(phone=phone)
    await state.set_state(OrderStates.entering_email)
    await message.answer("📧 Введите ваш email:")


@dp.message(F.chat.type == "private", StateFilter(OrderStates.entering_email))
async def enter_email(message: types.Message, state: FSMContext):
    email = message.text.strip()
    if not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", email):
        await message.answer("❌ Неверный email. Пример: example@mail.ru")
        return
    await state.update_data(email=email)

    data = await state.get_data()
    delivery_key = data.get("delivery_key", "")

    if delivery_key == "pickup":
        # Для самовывоза пропускаем адрес, устанавливаем "Самовывоз"
        await state.update_data(address="Самовывоз")
        await state.set_state(OrderStates.confirming)
        await show_confirmation(message, state)
    else:
        await state.set_state(OrderStates.entering_address)
        await message.answer("🏠 Введите адрес:")


@dp.message(F.chat.type == "private", StateFilter(OrderStates.entering_address))
async def enter_address(message: types.Message, state: FSMContext):
    address = message.text.strip()
    if not address:
        await message.answer("📍 Адрес не может быть пустым. Пожалуйста, введите адрес.")
        return

    await state.update_data(address=address)
    await state.set_state(OrderStates.confirming)
    await show_confirmation(message, state)


async def show_confirmation(message: types.Message, state: FSMContext):
    data = await state.get_data()
    cart = data.get("cart") or []
    # products_text с учётом количества
    products_text = "\n".join(
        f"• {get_product_by_id(pid)['name']} ({w}) × {qty} — {(price * qty) / 100:.0f}₽"
        for pid, w, price, qty in cart
    )
    total = data.get("order_total", 0)
    delivery_price = data.get("delivery_price", 0)
    final_total = data.get("final_total", 0)

    text = f"""
🧾 *Подтверждение заказа*

📦 *Товары:*
{products_text}

💵 *Цена товаров:* {total / 100:.0f}₽
🚚 *Доставка:* {data['delivery_option']} — {'бесплатно' if delivery_price == 0 else f'{delivery_price / 100:.0f}₽'}
💰 *Итого:* {final_total / 100:.0f}₽

☎️ *Телефон:* {data['phone']}
✉️ *Email:* {data['email']}
🏠 *Адрес:* {data['address']}

Нажмите *«Оплатить»*, чтобы завершить покупку 👇
    """.strip()

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить", callback_data="confirm_payment")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_order")]
    ])
    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")


@dp.callback_query(F.message.chat.type == "private", F.data == "confirm_payment")
async def confirm_payment(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    cart = data.get("cart") or []
    delivery_price = data.get("delivery_price", 0)
    delivery_option = data.get("delivery_option", "")

    # Строим prices как список LabeledPrice с суммами в копейках
    prices = []
    for pid, weight, price, qty in cart:
        item = get_product_by_id(pid)
        if item:
            item_amount = price * qty  # в копейках
            prices.append(LabeledPrice(label=f"{item['name']} ({weight}) x {qty}", amount=item_amount))

    if delivery_price > 0:
        prices.append(LabeledPrice(label=f"Доставка: {delivery_option}", amount=delivery_price))

    # Строим receipt items для provider_data
    items = []
    for pid, weight, price, qty in cart:
        item = get_product_by_id(pid)
        if item:
            item_unit_rub = price / 100
            items.append({
                "description": f"{item['name']} ({weight})",
                "quantity": str(qty),
                "amount": {"value": f"{item_unit_rub:.2f}", "currency": CURRENCY},
                "vat_code": 1
            })

    if delivery_price > 0:
        delivery_unit_rub = delivery_price / 100
        items.append({
            "description": f"Доставка: {delivery_option}",
            "quantity": "1",
            "amount": {"value": f"{delivery_unit_rub:.2f}", "currency": CURRENCY},
            "vat_code": 1
        })

    provider_data = {"receipt": {"items": items}}
    provider_data_json = json.dumps(provider_data)

    # Убрано сообщение о тестовой карте, так как теперь LIVE

    await bot.send_invoice(
        chat_id=callback.message.chat.id,
        title="💳 Оплата заказа",
        description="Хлеб + доставка",
        payload="order_paid",
        provider_token=PROVIDER_TOKEN,
        currency=CURRENCY,
        prices=prices,
        need_phone_number=True,
        send_phone_number_to_provider=True,
        provider_data=provider_data_json
    )


@dp.callback_query(F.message.chat.type == "private", F.data == "cancel_order")
async def cancel_order(callback: types.CallbackQuery, state: FSMContext):
    # Очищаем только данные заказа, оставляем cart
    await state.update_data(
        delivery_option=None,
        delivery_price=None,
        final_total=None,
        delivery_key=None,
        phone=None,
        email=None,
        address=None,
        order_total=None
    )
    await state.set_state(None)
    await callback.message.delete()
    await bot.send_message(callback.message.chat.id, "❌ Заказ отменён.", reply_markup=get_main_menu())


@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@dp.message(F.chat.type == "private", F.successful_payment)
async def process_successful_payment(message: types.Message, state: FSMContext):
    data = await state.get_data()
    # total в копейках в успешной оплате
    total = message.successful_payment.total_amount // 100
    order_id = int(time.time())

    cart = data.get("cart") or []
    products_text = "\n".join(
        f"• {get_product_by_id(pid)['name']} ({w}) × {qty} — {(price * qty) / 100:.0f}₽"
        for pid, w, price, qty in cart
    )

    # === СООБЩЕНИЕ АДМИНУ (без эмодзи) ===
    if ADMIN_ID:
        admin_text = f"""
Новый заказ #{order_id}!

Заказ #{order_id}
Товары:
{products_text}

Доставка: {data.get('delivery_option')} ({'бесплатно' if data.get('delivery_price', 0) == 0 else f"{data.get('delivery_price', 0) / 100:.0f}₽"})
Телефон: {data.get('phone')}
Email: {data.get('email')}
Адрес: {data.get('address')}
Сумма за товары: {data.get('order_total', 0) / 100:.0f}₽
Итого: {total:.0f}₽
        """.strip()
        try:
            await bot.send_message(ADMIN_ID, admin_text)
        except Exception as e:
            logging.error(f"Не удалось отправить админу: {e}")

    # === ЗАПИСЬ В GOOGLE SHEETS (без эмодзи в ячейках) ===
    service = get_sheets_service()
    if service:
        # формируем список товаров с количеством
        items_list = ", ".join(f"{get_product_by_id(pid)['name']} ({w}) × {qty}" for pid, w, _, qty in cart)
        row = [
            order_id,
            items_list,
            data.get("delivery_option", ""),
            data.get("phone", ""),
            data.get("email", ""),
            data.get("address", ""),
            f"{data.get('order_total', 0) / 100:.0f}",
            f"{data.get('delivery_price', 0) / 100:.0f}",
            f"{total:.0f}",
            datetime.now().strftime("%Y-%m-%d %H:%M")
        ]
        try:
            service.spreadsheets().values().append(
                spreadsheetId=SHEET_ID,
                range="A1",
                valueInputOption="RAW",
                body={"values": [row]}
            ).execute()
        except Exception as e:
            logging.error(f"Ошибка записи: {e}")

    # === УДАЛЕНИЕ Системных/старых сообщений (по возможности) ===
    try:
        for i in range(message.message_id - 20, message.message_id + 1):
            try:
                await bot.delete_message(message.chat.id, i)
            except:
                pass
    except:
        pass

    await bot.send_message(message.chat.id,
                           f"✅ *Оплата прошла успешно!*\n\n📦 Заказ №{order_id} принят.\nМы скоро свяжемся с вами ☎️",
                           reply_markup=get_main_menu(), parse_mode="Markdown")
    await state.clear()


@dp.callback_query(F.message.chat.type == "private", F.data == "back_to_menu")
async def back_to_menu(callback: types.CallbackQuery):
    await callback.message.delete()
    await bot.send_message(callback.message.chat.id, "🍞 Выберите категорию:", reply_markup=get_main_menu())


