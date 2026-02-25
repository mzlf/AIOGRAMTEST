import asyncio
import logging
import json
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from upstash_redis import Redis
from playwright.async_api import async_playwright

# --- КОНФИГ ---
TOKEN = "8313489502:AAF6uAPfnqtl__ls1okbIDVBC5t8rYs24oU"
REDIS_URL = "https://driven-fox-52037.upstash.io"
REDIS_TOKEN = "ActFAAIncDI4YzQwMjBhNzkxNzY0YmYzYjFhN2FmZGJkODg0NmFiMHAyNTIwMzc"

CITY, STREET, HOUSE = "с. Мала Михайлівка", "вул. Бесарабська", "32/"

bot = Bot(token=TOKEN)
dp = Dispatcher()
redis = Redis(url=REDIS_URL, token=REDIS_TOKEN)

logging.basicConfig(level=logging.INFO)

# Глобальные переменные браузера
browser = None
context = None
page = None
playwright = None
browser_lock = asyncio.Lock()

# =============================
# 🔥 JS анализ графика (Твой скрипт)
# =============================
analysis_script = """
() => {
    const activeTab = document.querySelector("#discon-fact .dates .date.active");
    const dateId = activeTab ? activeTab.getAttribute("rel") : null;

    const updateTimeElem = document.querySelector("#discon-fact .discon-fact-info-text");
    const updateTime = updateTimeElem ? updateTimeElem.innerText.trim() : "---";

    const row = document.querySelector("#discon-fact .discon-fact-table.active table tbody tr");
    if (!row) return { dateId, schedule: "Графік не знайдено", updateTime };

    const cells = Array.from(row.querySelectorAll("td")).slice(1, 25);

    let statuses = [];
    cells.forEach(c => {
        statuses.push((c.classList.contains('cell-scheduled') || c.classList.contains('cell-first-half')) ? "🔴" : "🟢");
        statuses.push((c.classList.contains('cell-scheduled') || c.classList.contains('cell-second-half')) ? "🔴" : "🟢");
    });

    let intervals = [];
    const fmt = (idx) => {
        let m = idx * 30;
        return String(Math.floor(m/60)).padStart(2,'0') + ":" + String(m%60).padStart(2,'0');
    };

    let cur = statuses[0], start = 0;
    for (let i = 1; i <= 48; i++) {
        if (i === 48 || statuses[i] !== cur) {
            intervals.push(cur + " <b>" + fmt(start) + " — " + (i === 48 ? "00:00" : fmt(i)) + "</b>");
            if(i < 48) { cur = statuses[i]; start = i; }
        }
    }

    return { dateId, schedule: intervals.join("\\n"), updateTime };
}
"""

# =============================
# 🌐 Логика браузера и Самовосстановление
# =============================
async def start_browser():
    global playwright, browser, context, page

    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
    context = await browser.new_context(user_agent="Mozilla/5.0")
    page = await context.new_page()

    await page.route(
        "**/*",
        lambda route: route.abort()
        if route.request.resource_type in ["image", "media", "font", "stylesheet"]
        else route.continue_()
    )

    await reload_page()


async def reload_page():
    global page
    logging.info("Перезагрузка страницы...")

    await page.goto("https://www.dtek-krem.com.ua/ua/shutdowns",
                    wait_until="networkidle",
                    timeout=60000)

    try:
        await page.click("button.modal__close", timeout=3000)
    except:
        pass

    for sel, val, lid in [
        ("input[name='city']", CITY, "city"),
        ("input[name='street']", STREET, "street"),
        ("input#house_num", HOUSE, "house_num"),
    ]:
        field = page.locator(sel).first
        await field.wait_for(state="visible", timeout=10000)
        await field.fill(val)
        try:
            await page.wait_for_selector(f"#{lid}autocomplete-list div", timeout=3000)
            await page.locator(f"#{lid}autocomplete-list div").first.click()
        except:
            await page.keyboard.press("ArrowDown")
            await page.keyboard.press("Enter")

    await page.wait_for_selector("#discon-fact", timeout=20000)


# =============================
# 📊 Получение всех вкладок
# =============================
async def get_all_schedules():
    async with browser_lock:
        try:
            result = {}

            tabs = page.locator("#discon-fact .dates .date")
            count = await tabs.count()

            for i in range(count):
                tab = tabs.nth(i)

                try:
                    await tab.click(timeout=5000)
                except:
                    logging.warning("Не удалось нажать вкладку, перезагружаю страницу")
                    await reload_page()
                    return await get_all_schedules()

                data = await page.evaluate(analysis_script)

                if data and data["dateId"]:
                    result[data["dateId"]] = data

            return result

        except Exception as e:
            logging.error(f"Ошибка получения графика: {e}")
            await reload_page()
            return {}


# =============================
# 🧹 Очистка старых ключей
# =============================
async def cleanup_old_keys(uid, active_rel_ids):
    keys = redis.keys(f"sched:{uid}:*")
    for key in keys:
        rel = key.split(":")[-1]
        if rel not in active_rel_ids:
         redis.delete(key)

async def monitoring_task():
    while True:
        await asyncio.sleep(60)

        users = redis.smembers("monitoring_users")
        if not users:
            continue

        schedules = await get_all_schedules()
        if not schedules:
            continue

        active_rel_ids = list(schedules.keys())

        for uid in users:
            changed_days = []

            for rel, data in schedules.items():
                cache_key = f"sched:{uid}:{rel}"
                cached = redis.get(cache_key)

                if cached is not None and cached != data["schedule"]:
                    changed_days.append(rel)

                redis.set(cache_key, data["schedule"], ex=172800)

            await cleanup_old_keys(uid, active_rel_ids)

            if changed_days:
                msg = "🔔 <b>ГРАФИК ИЗМЕНИЛСЯ!</b>\n\n"

                for rel in changed_days:
                    dt = datetime.fromtimestamp(int(rel))
                    date_str = dt.strftime("%d.%m.%Y")

                    msg += f"📅 <b>{date_str}</b>\n{schedules[rel]['schedule']}\n\n"

                msg += f"🕒 <i>Обновлено: {list(schedules.values())[0]['updateTime']}</i>"

                try:
                    await bot.send_message(int(uid), msg, parse_mode="HTML")
                except Exception as e:
                    logging.error(f"Ошибка отправки {uid}: {e}")

# =============================
# 🤖 Интерфейс Бота
# =============================
def get_kb(uid):
    return types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="Показати графік 💡")],
            [types.KeyboardButton(text="Вкл/Викл моніторинг 📡")]
        ], resize_keyboard=True
    )

@dp.message(Command("start"))
async def start_cmd(m: types.Message):
    await m.answer("Бот активний. Натисніть кнопку нижче для перевірки.", reply_markup=get_kb(m.from_user.id))

@dp.message(F.text.contains("мониторинг"))
async def toggle(m: types.Message):
    uid = str(m.from_user.id)

    if redis.sismember("monitoring_users", uid):
        redis.srem("monitoring_users", uid)
        await m.answer("Мониторинг выключен.")
    else:
        redis.sadd("monitoring_users", uid)
        await m.answer("Мониторинг включен.")

@dp.message(F.text.contains("график") | F.text.contains("Показати"))
async def manual(m: types.Message):
    msg = await m.answer("🔍 Проверяю сайт ДТЭК...")
    schedules = await get_all_schedules()

    if not schedules:
        await msg.edit_text("❌ Не вдалося отримати графік. Спробуйте пізніше.")
        return

    full_text = ""
    for rel in sorted(schedules.keys()):
        data = schedules[rel]
        try:
            dt = datetime.fromtimestamp(int(rel))
            date_str = dt.strftime("%d.%m.%Y")
        except: date_str = "Графік"
        full_text += f"📅 <b>{date_str}</b>\n{data['schedule']}\n\n"

    full_text += f"🕒 <i>Дані на: {list(schedules.values())[0]['updateTime']}</i>"
    await msg.edit_text(full_text, parse_mode="HTML")

async def main():
    await start_browser()
    asyncio.create_task(monitoring_task())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
