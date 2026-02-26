import asyncio
import logging
import json
from datetime import datetime, timedelta
import pytz
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from upstash_redis import Redis
from playwright.async_api import async_playwright

# --- КОНФИГ ---
TOKEN = "8766449770:AAENhr67_jjlh7CKFN_uj-SRI83Bu8ZP5xU"
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

# ТРЕКЕР ОБНОВЛЕНИЯ
last_full_reload = None  # Сюда пишем время последнего ввода адреса

# =============================
# 🔥 JS анализ графика
# =============================
analysis_script = """
() => {
    const activeTab = document.querySelector("#discon-fact .dates .date.active");
    const dateId = activeTab ? activeTab.getAttribute("rel") : null;
    const dateTextElem = activeTab ? activeTab.querySelector("div:nth-child(2)") : null;
    const dateText = dateTextElem ? dateTextElem.innerText.trim() : "Графік";

    const updateTimeElem = document.querySelector("#discon-fact .discon-fact-info-text");
    const updateTime = updateTimeElem ? updateTimeElem.innerText.trim() : "---";

    const row = document.querySelector("#discon-fact .discon-fact-table.active table tbody tr");
    if (!row) return { dateId, dateText, schedule: "Графік не знайдено", raw_statuses: [], updateTime };

    const cells = Array.from(row.querySelectorAll("td")).slice(1, 25);
    let raw_statuses = [];
    cells.forEach(c => {
        let s1 = (c.classList.contains('cell-scheduled') || c.classList.contains('cell-first-half')) ? "🔴" : "🟢";
        let s2 = (c.classList.contains('cell-scheduled') || c.classList.contains('cell-second-half')) ? "🔴" : "🟢";
        raw_statuses.push(s1, s2);
    });

    let intervals = [];
    const fmt = (idx) => {
        let m = idx * 30;
        return String(Math.floor(m/60)).padStart(2,'0') + ":" + String(m%60).padStart(2,'0');
    };

    let cur = raw_statuses[0], start = 0;
    for (let i = 1; i <= 48; i++) {
        if (i === 48 || raw_statuses[i] !== cur) {
            intervals.push(cur + " <b>" + fmt(start) + " — " + (i === 48 ? "00:00" : fmt(i)) + "</b>");
            if(i < 48) { cur = raw_statuses[i]; start = i; }
        }
    }

    return { dateId, dateText, schedule: intervals.join("\\n"), raw_statuses, updateTime };
}
"""

# =============================
# 🌐 Логика браузера
# =============================
async def start_browser():
    global playwright, browser, context, page
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
    context = await browser.new_context(user_agent="Mozilla/5.0")
    page = await context.new_page()

    # СВЕРХБЫСТРАЯ ЗАГРУЗКА: Блокируем всё, кроме самого важного
    await page.route("**/*", lambda route: route.abort() 
        if route.request.resource_type in ["image", "media", "font", "stylesheet", "other"] 
        or "google-analytics" in route.request.url 
        or "facebook" in route.request.url
        else route.continue_()
    )
    await reload_page()

async def reload_page():
    global page, last_full_reload
    logging.info("⚡ Быстрая перезагрузка страницы...")

    try:
        # Используем 'domcontentloaded' для мгновенного старта
        await page.goto("https://www.dtek-krem.com.ua/ua/shutdowns", 
                        wait_until="domcontentloaded", 
                        timeout=30000)
        
        # Модальное окно может не появиться без CSS, но проверим быстро
        try: await page.click("button.modal__close", timeout=500)
        except: pass

        # Ввод данных без лишних пауз
        for sel, val, lid in [
            ("input[name='city']", CITY, "city"),
            ("input[name='street']", STREET, "street"),
            ("input#house_num", HOUSE, "house_num"),
        ]:
            field = page.locator(sel).first
            await field.wait_for(state="attached", timeout=5000) # Ждем только появления в коде
            await field.fill(val)
            
            # Быстрый клик по автозаполнению
            try:
                # Ждем появления первого элемента списка
                item = page.locator(f"#{lid}autocomplete-list div").first
                await item.wait_for(state="attached", timeout=2000)
                await item.click()
            except:
                await page.keyboard.press("ArrowDown")
                await page.keyboard.press("Enter")

        # Ждем только сам блок графика
        await page.wait_for_selector("#discon-fact", timeout=10000)
        
        last_full_reload = datetime.now()
        logging.info(f"✅ Готово за {(datetime.now() - last_full_reload).total_seconds()} сек")
    except Exception as e:
        logging.error(f"❌ Ошибка быстрой загрузки: {e}")
# =============================
# 📊 Получение всех вкладок (с проверкой 5 минут)
# =============================
async def get_all_schedules():
    global last_full_reload
    async with browser_lock:
        # ПРОВЕРКА: Если прошло > 5 минут, перезагружаем принудительно
        if last_full_reload is None or (datetime.now() - last_full_reload) > timedelta(minutes=5):
            logging.info("⏱ Прошло более 5 минут с последнего обновления. Перезапуск...")
            await reload_page()

        try:
            result = {}
            tabs = page.locator("#discon-fact .dates .date")
            count = await tabs.count()

            if count == 0:
                await reload_page()
                return await get_all_schedules()

            for i in range(count):
                tab = tabs.nth(i)
                try:
                    await tab.click(timeout=5000)
                    await asyncio.sleep(0.5) 
                except:
                    await reload_page()
                    return await get_all_schedules()

                data = await page.evaluate(analysis_script)
                if data and data.get("dateId"):
                    result[data["dateId"]] = data

            return result
        except Exception as e:
            logging.error(f"Ошибка получения графика: {e}")
            await reload_page()
            return {}

# =============================
# ⏳ Логика расчета времени
# =============================
def calculate_time_left(raw_statuses):
    if not raw_statuses or len(raw_statuses) < 48:
        return "Нет данных для расчета."

    tz = pytz.timezone('Europe/Kiev')
    now = datetime.now(tz)
    minutes_from_start = now.hour * 60 + now.minute
    current_idx = minutes_from_start // 30
    
    if current_idx >= 48:
        return "Сегодняшний график закончился."

    current_state = raw_statuses[current_idx]
    change_idx = -1
    for i in range(current_idx + 1, 48):
        if raw_statuses[i] != current_state:
            change_idx = i
            break
    
    if change_idx == -1:
        return f"Сейчас {current_state}. До конца дня статус не изменится."

    diff_minutes = (change_idx * 30) - minutes_from_start
    hours = diff_minutes // 60
    minutes = diff_minutes % 60
    
    action = "включат" if current_state == "🔴" else "выключат"
    time_str = f"<b>{hours} час. {minutes} м.</b>" if hours > 0 else f"<b>{minutes} м.</b>"
    
    return f"Сейчас: {current_state}\nПриблизительно через {time_str} свет {action}."

# =============================
# 🧹 Мониторинг и Бот
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
        if not users: continue

        schedules = await get_all_schedules()
        if not schedules: continue

        for uid in users:
            changed_days = []
            for rel, data in schedules.items():
                cache_key = f"sched:{uid}:{rel}"
                cached = redis.get(cache_key)
                if cached is not None and cached != data["schedule"]:
                    changed_days.append(rel)
                redis.set(cache_key, data["schedule"], ex=172800)

            if changed_days:
                msg = "🔔 <b>ГРАФИК ИЗМЕНИЛСЯ!</b>\n\n"
                for rel in changed_days:
                    dt = datetime.fromtimestamp(int(rel))
                    msg += f"📅 <b>{dt.strftime('%d.%m.%Y')}</b>\n{schedules[rel]['schedule']}\n\n"
                msg += f"🕒 <i>Обновлено: {list(schedules.values())[0]['updateTime']}</i>"
                try: await bot.send_message(int(uid), msg, parse_mode="HTML")
                except: pass

def get_kb(uid):
    return types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="Показать график 💡")],
            [types.KeyboardButton(text="Вкл/Выкл мониторинг 📡")],
        ], resize_keyboard=True
    )

@dp.message(Command("start"))
async def start_cmd(m: types.Message):
    await m.answer("Бот работает.", reply_markup=get_kb(m.from_user.id))

@dp.message(F.text.contains("мониторинг"))
async def toggle(m: types.Message):
    uid = str(m.from_user.id)
    if redis.sismember("monitoring_users", uid):
        redis.srem("monitoring_users", uid)
        await m.answer("Мониторинг выключен.")
    else:
        redis.sadd("monitoring_users", uid)
        await m.answer("Мониторинг включен.")

@dp.message(F.text.contains("график") | F.text.contains("Показать"))
async def manual(m: types.Message):
    msg = await m.answer("🔍 Проверяю сайт ДТЭК...")
    schedules = await get_all_schedules()
    
    if not schedules:
        await msg.edit_text("❌ Не удалось получить график.")
        return

    today_rel = sorted(schedules.keys())[0]
    data = schedules[today_rel]
    ans = calculate_time_left(data.get('raw_statuses', []))
    
    full_text = ""
    for rel in sorted(schedules.keys()):
        d = schedules[rel]
        full_text += f"⚡ <b>{d['dateText']}</b>\n{d['schedule']}\n\n"

    full_text += f"🕒 <i>{list(schedules.values())[0]['updateTime']}</i>\n\n"
    full_text += f"{ans}"
    
    await msg.edit_text(full_text, parse_mode="HTML")

async def main():   
    await start_browser()
    asyncio.create_task(monitoring_task())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
