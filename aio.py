import os
import asyncio
import logging
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from upstash_redis import Redis  # Твоя библиотека
from playwright.async_api import async_playwright

# --- КОНФИГУРАЦИЯ ---
TOKEN = "8766449770:AAENhr67_jjlh7CKFN_uj-SRI83Bu8ZP5xU"
# Твой Upstash (лучше вынести в переменные Railway)
REDIS_URL = "https://driven-fox-52037.upstash.io"
REDIS_TOKEN = "ActFAAIncDI4YzQwMjBhNzkxNzY0YmYzYjFhN2FmZGJkODg0NmFiMHAyNTIwMzc"

CITY = "с. Мала Михайлівка"
STREET = "вул. Бесарабська"
HOUSE = "32/"

# Инициализация
bot = Bot(token=TOKEN)
dp = Dispatcher()
# Upstash Redis (синхронный клиент внутри асинхронных функций работает ок)
redis = Redis(url=REDIS_URL, token=REDIS_TOKEN)
browser_lock = asyncio.Lock()

logging.basicConfig(level=logging.INFO)

# --- ПАРСЕР (ASYNC) ---
async def get_dtek_full_data():
    async with browser_lock:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = await browser.new_context(user_agent="Mozilla/5.0")
            page = await context.new_page()
            
            # Блокируем картинки для скорости
            await page.route("**/*.{png,jpg,jpeg,svg,woff,woff2}", lambda route: route.abort())

            try:
                await page.goto("https://www.dtek-krem.com.ua/ua/shutdowns", wait_until="networkidle", timeout=60000)
                try: await page.click("button.modal__close", timeout=5000)
                except: pass

                async def safe_fill(selector, value, list_id):
                    f = page.locator(selector).first
                    await f.wait_for(state="visible", timeout=15000)
                    await f.click()
                    await page.keyboard.press("Control+A")
                    await page.keyboard.press("Backspace")
                    await f.fill(value)
                    
                    s = f"#{list_id}autocomplete-list div, .autocomplete-suggestion:visible"
                    try:
                        await page.wait_for_selector(s, state="visible", timeout=10000)
                        await page.locator(s).first.click(force=True)
                    except:
                        await page.keyboard.press("ArrowDown")
                        await page.keyboard.press("Enter")

                await safe_fill("input[name='city']", CITY, "city")
                await safe_fill("input[name='street']", STREET, "street")
                await safe_fill("input#house_num, input[name='house']", HOUSE, "house_num")

                await page.wait_for_selector("#discon-fact", timeout=20000)

                analysis_script = """
                () => {
                    const updateTimeElem = document.querySelector("#discon-fact > div.discon-fact-info > span.discon-fact-info-text");
                    const updateTime = updateTimeElem ? updateTimeElem.innerText.replace("Дата та час останнього оновлення інформації на графіку:", "").trim() : "---";
                    const row = document.querySelector("#discon-fact > div.discon-fact-tables > div.discon-fact-table.active > table > tbody > tr");
                    if (!row) return { update_time: updateTime, schedule: "✅ Свет по графику" };
                    const cells = Array.from(row.querySelectorAll("td")).slice(1, 25);
                    let halfStatuses = [];
                    cells.forEach(cell => {
                        let f = cell.classList.contains('cell-scheduled') || cell.classList.contains('cell-first-half');
                        let s = cell.classList.contains('cell-scheduled') || cell.classList.contains('cell-second-half');
                        halfStatuses.push(f ? "🔴" : "🟢"); halfStatuses.push(s ? "🔴" : "🟢");
                    });
                    let intervals = [];
                    let cur = halfStatuses[0]; let start = 0;
                    const fmt = (idx) => {
                        let m = idx * 30;
                        return String(Math.floor(m/60)).padStart(2,'0') + ":" + String(m%60).padStart(2,'0');
                    };
                    for (let i = 1; i <= 48; i++) {
                        if (i === 48 || halfStatuses[i] !== cur) {
                            intervals.push(cur + " <b>" + fmt(start) + " — " + (i === 48 ? "00:00" : fmt(i)) + "</b>");
                            if (i < 48) { cur = halfStatuses[i]; start = i; }
                        }
                    }
                    return { update_time: updateTime, schedule: intervals.join('\\n') };
                }
                """
                today_res = await page.evaluate(analysis_script)

                tomorrow_res = {"update_time": "---", "schedule": "График еще не опубликован"}
                tomorrow_tab = page.locator("#discon-fact > div.dates > div:nth-child(2)")
                if await tomorrow_tab.is_visible():
                    await tomorrow_tab.click()
                    tomorrow_res = await page.evaluate(analysis_script)

                await browser.close()
                return {"today": today_res, "tomorrow": tomorrow_res}
            except Exception as e:
                if 'browser' in locals(): await browser.close()
                logging.error(f"Парсинг упал: {e}")
                return None

# --- МОНИТОРИНГ (ASYNC + UPSTASH) ---
async def monitoring_task():
    while True:
        await asyncio.sleep(300) # Проверка каждые 5 минут
        
        # Получаем всех пользователей из Set
        users = redis.smembers("monitoring_users")
        if not users: continue

        res = await get_dtek_full_data()
        if not res: continue

        now = datetime.now()
        t_str = now.strftime("%d.%m.%Y")
        tm_str = (now + timedelta(days=1)).strftime("%d.%m.%Y")

        for uid in users:
            update_parts = []
            # Проверка Сегодня
            cached_today = redis.get(f"sched:{uid}:{t_str}")
            if res['today']['schedule'] != cached_today:
                if cached_today: # Не шлем первый раз
                    update_parts.append(f"📅 <b>Сегодня ({t_str}):</b>\n{res['today']['schedule']}")
                redis.set(f"sched:{uid}:{t_str}", res['today']['schedule'], ex=172800)

            # Проверка Завтра
            if "не опубликован" not in res['tomorrow']['schedule'].lower():
                cached_tomorrow = redis.get(f"sched:{uid}:{tm_str}")
                if res['tomorrow']['schedule'] != cached_tomorrow:
                    update_parts.append(f"📅 <b>Завтра ({tm_str}):</b>\n{res['tomorrow']['schedule']}")
                    redis.set(f"sched:{uid}:{tm_str}", res['tomorrow']['schedule'], ex=172800)

            if update_parts:
                msg = "🔔 <b>ГРАФИК ИЗМЕНИЛСЯ!</b>\n\n" + "\n\n".join(update_parts)
                msg += f"\n\n🕒 <i>Обновлено: {res['today']['update_time']}</i>"
                try: await bot.send_message(int(uid), msg, parse_mode="HTML")
                except: pass

# --- КЛАВИАТУРА ---
def main_kb(uid):
    is_mon = redis.sismember("monitoring_users", str(uid))
    kb = [
        [types.KeyboardButton(text="Сегодня 💡"), types.KeyboardButton(text="Завтра 📅")],
        [types.KeyboardButton(text="Выключить мониторинг ❌" if is_mon else "Включить мониторинг 📡")]
    ]
    return types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

# --- ХЕНДЛЕРЫ ---
@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer("💡 Бот мониторинга ДТЭК (Async + Redis)", 
                         reply_markup=main_kb(message.from_user.id))

@dp.message(F.text.contains("мониторинг"))
async def toggle_mon(message: types.Message):
    uid = str(message.from_user.id)
    if redis.sismember("monitoring_users", uid):
        redis.srem("monitoring_users", uid)
        await message.answer("📴 Мониторинг выключен.", reply_markup=main_kb(uid))
    else:
        redis.sadd("monitoring_users", uid)
        await message.answer("📡 Мониторинг включен! Я пришлю уведомление, если график на сегодня или завтра изменится.", 
                             reply_markup=main_kb(uid))

@dp.message(F.text.in_(["Сегодня 💡", "Завтра 📅"]))
async def get_manual(message: types.Message):
    status = await message.answer("🔍 Запрашиваю сайт ДТЭК...")
    res = await get_dtek_full_data()
    if res:
        day = "tomorrow" if "Завтра" in message.text else "today"
        data = res[day]
        resp = f"<b>🕒 Обновлено:</b> {data['update_time']}\n\n<b>📢 График:</b>\n\n{data['schedule']}"
        await bot.edit_message_text(text=resp, chat_id=message.chat.id, message_id=status.message_id, parse_mode="HTML") 
    else:
        await bot.edit_message_text("❌ Ошибка связи с ДТЭК.", chat_id=message.chat.id, message_id=status.message_id)

async def main():
    asyncio.create_task(monitoring_task())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
