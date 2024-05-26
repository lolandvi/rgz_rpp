import os
import logging
from dotenv import load_dotenv
import asyncio
from datetime import datetime
import psycopg2
import aiohttp
from aiogram import Bot, Dispatcher, types, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.filters.state import State, StatesGroup

load_dotenv()
router = Router()

# Подключение к базе данных Postgres
conn = psycopg2.connect(
    dbname="rgz",
    user="postgres",
    password="postgres",
    host="localhost"
)
cursor = conn.cursor()

# Создание таблиц users и operations, budget
cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        name VARCHAR(255),
        chat_id BIGINT
    )
""")
cursor.execute("""
    CREATE TABLE IF NOT EXISTS operations (
        id SERIAL PRIMARY KEY,
        date DATE,
        sum FLOAT,
        chat_id BIGINT,
        type_operation VARCHAR(50)
    )
""")
cursor.execute("""
    CREATE TABLE IF NOT EXISTS budget (
        id SERIAL PRIMARY KEY,
        month INTEGER,
        budget FLOAT,
        chat_id BIGINT
    )
""")
conn.commit()

API_TOKEN = os.getenv('API_TOKEN')
logging.basicConfig(level=logging.INFO)

# Инициализация бота
bot = Bot(token=API_TOKEN)
dp = Dispatcher()
dp.include_router(router)

class Registration(StatesGroup):
    waiting_for_name = State()

# Регистрация пользователя
@router.message(StateFilter(None), Command('reg'))
async def register_user(message: types.Message, state: FSMContext):
    cursor.execute("SELECT * FROM users WHERE chat_id = %s", (message.from_user.id,))
    if cursor.fetchone():
        await message.answer("Вы уже зарегистрированы.")
    else:
        await message.answer("Пожалуйста, введите ваш логин:")
        await state.set_state(Registration.waiting_for_name)

@router.message(Registration.waiting_for_name)
async def process_name(message: types.Message, state: FSMContext):
    data = await state.get_data()
    data['name'] = message.text
    
    cursor.execute("INSERT INTO users (name, chat_id) VALUES (%s, %s)", (data['name'], message.from_user.id))
    conn.commit()
    await state.clear()
    await message.answer(f"Вы успешно зарегистрированы под именем {data['name']}!")

# Операции РАСХОД/ДОХОД
class AddOperation(StatesGroup):
    waiting_for_operation_type = State()
    waiting_for_amount = State()
    waiting_for_date = State()
    waiting_for_currency = State()
    waiting_for_exchange = State()

@router.message(StateFilter(None), Command('add_operation'))
async def add_operation_start(message: types.Message, state: FSMContext):
    cursor.execute("SELECT * FROM users WHERE chat_id = %s", (message.from_user.id,))
    if not cursor.fetchone():
        await message.answer("Пожалуйста, зарегистрируйтесь сначала.")
        return
    
    kbb = [
        [types.KeyboardButton(text="РАСХОД")],
        [types.KeyboardButton(text="ДОХОД")]
    ]

    markupp = types.ReplyKeyboardMarkup(keyboard=kbb)
    await message.answer("Выберите тип операции:", reply_markup=markupp)
    await state.set_state(AddOperation.waiting_for_operation_type)

@router.message(AddOperation.waiting_for_operation_type)
async def process_operation_type(message: types.Message, state: FSMContext):
    operation_type = message.text
    await state.update_data(operation_type=operation_type)
    await message.answer("Введите сумму операции в рублях:")
    await state.set_state(AddOperation.waiting_for_amount)

@router.message(AddOperation.waiting_for_amount)
async def process_amount(message: types.Message, state: FSMContext):
    sum = float(message.text)
    data = await state.get_data()
    data['sum'] = sum
    await state.update_data(data)
    await message.answer("Укажите дату операции (в формате ГГГГ-ММ-ДД):")
    await state.set_state(AddOperation.waiting_for_date)

@router.message(AddOperation.waiting_for_date)
async def process_date(message: types.Message, state: FSMContext):
    try:
        date = datetime.strptime(message.text, '%Y-%m-%d').date()
    except ValueError:
        await message.answer("Неверный формат даты. Попробуйте снова.")
        return
    
    data = await state.get_data()
    cursor.execute("INSERT INTO operations (date, sum, chat_id, type_operation) VALUES (%s, %s, %s, %s)",
                   (date, data['sum'], message.from_user.id, data['operation_type']))
    conn.commit()
    
    await state.clear()
    await message.answer("Операция успешно добавлена.")

# Подключение к внешнему сервису для конвертации операций и бюджета в доллары и евро
async def get_exchange_rate(currency):
    url = f"http://195.58.54.159:8000/rate?currency={currency}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status == 200:
                data = await response.json()
                return data.get('rate')
            elif response.status == 400:
                return "UNKNOWN CURRENCY"
            elif response.status == 500:
                return "UNEXPECTED ERROR"
            else:
                return None
            
# Выбор валюты в которую необходимо выполнить конвертацию
@router.message(StateFilter(None), Command('operations'))
async def show_operations_menu(message: types.Message, state: FSMContext):
    cursor.execute("SELECT * FROM users WHERE chat_id = %s", (message.from_user.id,))
    user = cursor.fetchone()
    if not user:
        await message.answer("Пожалуйста, зарегистрируйтесь сначала.")
        return

    kb = [
            [types.KeyboardButton(text="USD")],
            [types.KeyboardButton(text="RUB")],
            [types.KeyboardButton(text="EUR")]
        ]

    markup = types.ReplyKeyboardMarkup(keyboard=kb)
    await message.answer("Выберите валюту:", reply_markup=markup)
    await state.set_state(AddOperation.waiting_for_currency)

@router.message(AddOperation.waiting_for_currency)
async def process_currency_choice(message: types.Message, state: FSMContext):
    currency = message.text
    if currency not in ["RUB", "EUR", "USD"]:
        await message.answer("Пожалуйста, выберите одну из предложенных валют.")
        return
    
    if currency == "RUB":
        await state.update_data(currency=currency)
        await process_operations(message, state)
        return

    exchange_rate = await get_exchange_rate(currency)
    if exchange_rate is None:
        await message.answer(f"Курс обмена для {currency} не найден")
        return
    
    await state.update_data(currency=currency)
    await state.set_state(AddOperation.waiting_for_exchange)

# Конвертируем
@router.message(AddOperation.waiting_for_exchange)
async def process_operations(message: types.Message, state: FSMContext):
    data = await state.get_data()
    cursor.execute("SELECT * FROM operations WHERE chat_id = %s", (message.from_user.id,))
    operations = cursor.fetchall()

    currency = data.get('currency')
    if currency == 'RUB':
        response = "\nИнформация по операциям:\n"
        for operation in operations:
            response += f"Дата: {operation[1]}, Сумма: {operation[2]}, Тип: {operation[4]}\n"

        budget = 0
        cursor.execute("SELECT budget FROM budget WHERE chat_id = %s AND month = %s", (message.from_user.id, datetime.now().month))
        budget_row = cursor.fetchone()
        if budget_row:
            budget = budget_row[0]

        remaining_budget = await calculate_remaining_budget(message.from_user.id)  # Calculate remaining budget

        if remaining_budget is not None:
            await message.answer(f"Бюджет на текущий месяц: {budget}\nОстаток средств в бюджете: {remaining_budget}\n{response}\n")
        else:
            await message.answer(f"Бюджет на текущий месяц: {budget}\n{response}\n")
        return

    exchange_rate = await get_exchange_rate(currency)
    
    if exchange_rate is None:
        await message.answer(f"Курс обмена для {currency} не найден")
        return

    converted_operations = []
    for operation in operations:
        converted_amount = operation[2] / exchange_rate
        converted_operations.append((operation[1], converted_amount, operation[4]))

    response = "\nИнформация по операциям в выбранной валюте:\n"
    for converted_operation in converted_operations:
        response += f"Дата: {converted_operation[0]}, Сумма: {converted_operation[1]}, Тип: {converted_operation[2]}\n"

    budget = 0
    cursor.execute("SELECT budget FROM budget WHERE chat_id = %s AND month = %s", (message.from_user.id, datetime.now().month))
    budget_row = cursor.fetchone()
    if budget_row:
        budget = budget_row[0]
        budget = budget/exchange_rate

    converted_budget = budget

    remaining_budget = await calculate_remaining_budget(message.from_user.id)

    if remaining_budget is not None:
        await message.answer(f"Бюджет на текущий месяц: {converted_budget}\nОстаток средств в бюджете: {remaining_budget/exchange_rate}\n{response}\n")
    else:
        await message.answer(f"Бюджет на текущий месяц: {converted_budget}\n{response}\n")

    await state.clear()

# Установка бюджета
@router.message(StateFilter(None), Command('setbudget'))
async def set_budget_start(message: types.Message, state: FSMContext):
    cursor.execute("SELECT * FROM users WHERE chat_id = %s", (message.from_user.id,))
    if not cursor.fetchone():
        await message.answer("Пожалуйста, зарегистрируйтесь сначала.")
        return

    await message.answer("Введите бюджет на текущий месяц:")
    await state.set_state(AddBudget.waiting_for_budget)


class AddBudget(StatesGroup):
    waiting_for_budget = State()

@router.message(AddBudget.waiting_for_budget)
async def process_budget(message: types.Message, state: FSMContext):
    try:
        budget = float(message.text)
    except ValueError:
        await message.answer("Неверный формат суммы. Попробуйте снова.")
        return

    data = await state.get_data()
    data['budget'] = budget
    await state.update_data(data)

    cursor.execute("INSERT INTO budget (month, budget, chat_id) VALUES (%s, %s, %s)",
                   (datetime.now().month, budget, message.from_user.id))
    conn.commit()

    await state.clear()
    await message.answer("Информация о бюджете успешно сохранена.")

# Рассчитываем сколько осталось в бюджете исходя из РАСХОДОВ и ДОХОДОВ
async def calculate_remaining_budget(chat_id):
    cursor.execute("SELECT sum FROM operations WHERE chat_id = %s AND type_operation = 'ДОХОД'", (chat_id,))
    income_data = cursor.fetchall()
    total_income = sum(income[0] for income in income_data)

    cursor.execute("SELECT sum FROM operations WHERE chat_id = %s AND type_operation = 'РАСХОД'", (chat_id,))
    expense_data = cursor.fetchall()
    total_expense = sum(expense[0] for expense in expense_data)

    cursor.execute("SELECT budget FROM budget WHERE chat_id = %s AND month = %s", (chat_id, datetime.now().month))
    budget_row = cursor.fetchone()
    if budget_row:
        budget = budget_row[0]
        remaining_budget = budget - total_expense + total_income
        return remaining_budget
    return None

async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())