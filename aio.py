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

# Переменные браузера
playwright = None
browser = None
# Две отдельные страницы и два замка
page_monitor = None
page_user = None
lock_monitor = asyncio.Lock()
lock_user = asyncio.Lock()

# ТРЕКЕР ОБНОВЛЕНИЯ ДЛЯ МОНИТОРИНГА
last_monitor_reload = None

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
async def setup_page(ctx):
    p = await ctx.new_page()
    # Блокируем лишнее для скорости
    await p.route("**/*", lambda route: route.abort() 
        if route.request.resource_type in ["image", "media", "font", "stylesheet", "other"] 
        else route.continue_()
    )
    return p

async def start_browser():
    global playwright, browser, page_monitor, page_user
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
    context = await browser.new_context(user_agent="Mozilla/5.0")
    
    page_monitor = await setup_page(context)
    page_user = await setup_page(context)
    
    # Первичная загрузка
    await reload_page(page_monitor)
    await reload_page(page_user)

async def reload_page(p):
    logging.info(f"⚡ Перезагрузка и ввод адреса на странице...")
    try:
        await p.goto("https://www.dtek-krem.com.ua/ua/shutdowns", wait_until="domcontentloaded", timeout=30000)
        try: await p.click("button.modal__close", timeout=500)
        except: pass

        for sel, val, lid in [("input[name='city']", CITY, "city"), ("input[name='street']", STREET, "street"), ("input#house_num", HOUSE, "house_num")]:
            field = p.locator(sel).first
            await field.wait_for(state="attached", timeout=5000)
            await field.fill(val)
            try:
                item = p.locator(f"#{lid}autocomplete-list div").first
                await item.wait_for(state="attached", timeout=2000)
                await item.click()
            except:
                await p.keyboard.press("ArrowDown")
                await p.keyboard.press("Enter")
        await p.wait_for_selector("#discon-fact", timeout=10000)
    except Exception as e:
        logging.error(f"❌ Ошибка перезагрузки: {e}")

# =============================
# 📊 Универсальный парсер
# =============================
async def fetch_data(p, lock, force=False):
    async with lock:
        if force:
            await reload_page(p)
        
        try:
            result = {}
            tabs = p.locator("#discon-fact .dates .date")
            count = await tabs.count()
            if count == 0: 
                await reload_page(p)
                return await fetch_data(p, lock, force=False)

            for i in range(count):
                tab = tabs.nth(i)
                await tab.click(timeout=5000)
                await asyncio.sleep(0.3)
                data = await p.evaluate(analysis_script)
                if data and data.get("dateId"):
                    result[data["dateId"]] = data
            return result
        except:
            return {}

# =============================
# ⏳ Расчет времени (Остается без изменений)
# =============================
def calculate_time_left(raw_statuses):
    if not raw_statuses: return "Нет данных."
    tz = pytz.timezone('Europe/Kiev')
    now = datetime.now(tz)
    m_now = now.hour * 60 + now.minute
    idx = m_now // 30
    if idx >= 48: return "День окончен."
    curr = raw_statuses[idx]
    target_idx = -1
    for i in range(idx + 1, 48):
        if raw_statuses[i] != curr:
            target_idx = i
            break
    if target_idx == -1: return f"Сейчас {curr}. До 00:00 без изменений."
    diff = (target_idx * 30) - m_now
    h, m = diff // 60, diff % 60
    act = "включат" if curr == "🔴" else "выключат"
    return f"Сейчас: {curr}\nЧерез <b>{h}ч. {m}м.</b> свет {act}."

# =============================
# 📡 Мониторинг (КД 60 сек)
# =============================
async def monitoring_task():
    global last_monitor_reload
    while True:
        await asyncio.sleep(10) # Проверяем список юзеров часто
        users = redis.smembers("monitoring_users")
        if not users: continue

        # Проверка КД 60 секунд для обновления страницы мониторинга
        now = datetime.now()
        should_reload = False
        if last_monitor_reload is None or (now - last_monitor_reload) > timedelta(seconds=60):
            should_reload = True
            last_monitor_reload = now

        schedules = await fetch_data(page_monitor, lock_monitor, force=should_reload)
        if not schedules: continue

        for uid in users:
            uid = uid.decode() if isinstance(uid, bytes) else uid
            changed = []
            for rel, data in schedules.items():
                cache_key = f"sched:{uid}:{rel}"
                cached = redis.get(cache_key)
                if cached is not None and cached.decode() != data["schedule"]:
                    changed.append(rel)
                redis.set(cache_key, data["schedule"], ex=172800)

            if changed:
                msg = "🔔 <b>ГРАФИК ИЗМЕНИЛСЯ!</b>\n\n"
                for rel in sorted(schedules.keys()):
                    d = schedules[rel]
                    msg += f"⚡ <b>{d['dateText']}</b>\n{d['schedule']}\n\n"
                msg += f"🕒 <i>Обновлено: {list(schedules.values())[0]['updateTime']}</i>"
                try: await bot.send_message(int(uid), msg, parse_mode="HTML")
                except: pass

# =============================
# 🤖 Обработка юзера
# =============================
@dp.message(F.text.contains("график") | F.text.contains("Показать"))
async def manual(m: types.Message):
    msg = await m.answer("🔍 Проверяю сайт (полное обновление)...")
    # Для юзера ВСЕГДА force=True
    schedules = await fetch_data(page_user, lock_user, force=True)
    
    if not schedules:
        await msg.edit_text("❌ Не удалось получить данные.")
        return

    today_rel = sorted(schedules.keys())[0]
    ans = calculate_time_left(schedules[today_rel].get('raw_statuses', []))
    
    full_text = ""
    for rel in sorted(schedules.keys()):
        d = schedules[rel]
        full_text += f"⚡ <b>{d['dateText']}</b>\n{d['schedule']}\n\n"

    full_text += f"🕒 <i>{list(schedules.values())[0]['updateTime']}</i>\n\n{ans}"
    await msg.edit_text(full_text, parse_mode="HTML")

# ... остальной код (get_kb, toggle, start_cmd, main) такой же ...
def get_kb(uid):
    return types.ReplyKeyboardMarkup(
        keyboard=[[types.KeyboardButton(text="Показать график 💡")], [types.KeyboardButton(text="Вкл/Выкл мониторинг 📡")]], 
        resize_keyboard=True
    )

@dp.message(Command("start"))
async def start_cmd(m: types.Message):
    await m.answer("Бот запущен.", reply_markup=get_kb(m.from_user.id))

@dp.message(F.text.contains("мониторинг"))
async def toggle(m: types.Message):
    uid = str(m.from_user.id)
    if redis.sismember("monitoring_users", uid):
        redis.srem("monitoring_users", uid)
        await m.answer("Мониторинг выключен.")
    else:
        redis.sadd("monitoring_users", uid)
        await m.answer("Мониторинг включен.")

async def main():   
    await start_browser()
    asyncio.create_task(monitoring_task())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
