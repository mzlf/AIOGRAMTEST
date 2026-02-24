import os
import asyncio
import logging
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from upstash_redis import Redis
from playwright.async_api import async_playwright

# --- КОНФИГУРАЦИЯ ---
TOKEN = "8766449770:AAENhr67_jjlh7CKFN_uj-SRI83Bu8ZP5xU"
REDIS_URL = "https://driven-fox-52037.upstash.io"
REDIS_TOKEN = "ActFAAIncDI4YzQwMjBhNzkxNzY0YmYzYjFhN2FmZGJkODg0NmFiMHAyNTIwMzc"

CITY, STREET, HOUSE = "с. Мала Михайлівка", "вул. Бесарабська", "32/"

bot = Bot(token=TOKEN)
dp = Dispatcher()
redis = Redis(url=REDIS_URL, token=REDIS_TOKEN)
browser_lock = asyncio.Lock()

logging.basicConfig(level=logging.INFO)

async def get_dtek_full_data():
    async with browser_lock:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False, args=["--no-sandbox"])
            context = await browser.new_context(user_agent="Mozilla/5.0")
            page = await context.new_page()
            await page.route("**/*.{png,jpg,jpeg,svg,woff,woff2}", lambda route: route.abort())

            try:
                await page.goto("https://www.dtek-krem.com.ua/ua/shutdowns", wait_until="networkidle", timeout=60000)
                try: await page.click("button.modal__close", timeout=5000)
                except: pass

                # Логика ввода адреса (уже проверенная)
                for sel, val, lid in [("input[name='city']", CITY, "city"), 
                                      ("input[name='street']", STREET, "street"), 
                                      ("input#house_num", HOUSE, "house_num")]:
                    f = page.locator(sel).first
                    await f.wait_for(state="visible")
                    await f.click()
                    await page.keyboard.press("Control+A")
                    await page.keyboard.press("Backspace")
                    await f.fill(val)
                    try:
                        await page.wait_for_selector(f"#{lid}autocomplete-list div", state="visible", timeout=5000)
                        await page.locator(f"#{lid}autocomplete-list div").first.click()
                    except:
                        await page.keyboard.press("ArrowDown")
                        await page.keyboard.press("Enter")

                await page.wait_for_selector("#discon-fact", timeout=20000)

                # СКРИПТ АНАЛИЗА (теперь тянет дату-ID из rel)
                analysis_script = """
                () => {
                    const activeTab = document.querySelector("#discon-fact > div.dates > div.active");
                    const dateId = activeTab ? activeTab.getAttribute('rel') : null;
                    const dateText = activeTab ? activeTab.innerText.trim() : "";
                    
                    const updateTimeElem = document.querySelector("#discon-fact > div.discon-fact-info > span.discon-fact-info-text");
                    const updateTime = updateTimeElem ? updateTimeElem.innerText.replace("Дата та час останнього оновлення інформації на графіку:", "").trim() : "---";
                    
                    const row = document.querySelector("#discon-fact > div.discon-fact-tables > div.discon-fact-table.active > table > tbody > tr");
                    if (!row) return { dateId, dateText, updateTime, schedule: "График не найден" };
                    
                    const cells = Array.from(row.querySelectorAll("td")).slice(1, 25);
                    let statuses = [];
                    cells.forEach(c => {
                        statuses.push((c.classList.contains('cell-scheduled') || c.classList.contains('cell-first-half')) ? "🔴" : "🟢");
                        statuses.push((c.classList.contains('cell-scheduled') || c.classList.contains('cell-second-half')) ? "🔴" : "🟢");
                    });
                    
                    let intervals = [];
                    const fmt = (idx) => { let m = idx*30; return String(Math.floor(m/60)).padStart(2,'0')+":"+String(m%60).padStart(2,'0'); };
                    let cur = statuses[0], start = 0;
                    for(let i=1; i<=48; i++) {
                        if(i===48 || statuses[i] !== cur) {
                            intervals.push(cur + " <b>" + fmt(start) + " — " + (i===48 ? "00:00" : fmt(i)) + "</b>");
                            cur = statuses[i]; start = i;
                        }
                    }
                    return { dateId, dateText, updateTime, schedule: intervals.join('\\n') };
                }
                """
                
                # Данные за сегодня
                today_data = await page.evaluate(analysis_script)
                
                # Данные за завтра
                tomorrow_data = None
                tomorrow_btn = page.locator("#discon-fact > div.dates > div:nth-child(2)")
                if await tomorrow_btn.is_visible():
                    await tomorrow_btn.click()
                    tomorrow_data = await page.evaluate(analysis_script)

                await browser.close()
                return {"today": today_data, "tomorrow": tomorrow_data}
            except Exception as e:
                await browser.close()
                logging.error(f"Ошибка: {e}")
                return None

# --- УМНЫЙ МОНИТОРИНГ ---
async def monitoring_task():
    while True:
        await asyncio.sleep(300) # Проверка каждые 5 минут
        users = redis.smembers("monitoring_users")
        if not users: continue

        res = await get_dtek_full_data()
        if not res: continue

        for uid in users:
            changed = False
            
            # 1. Проверяем, изменилось ли хоть что-то
            for day_key in ['today', 'tomorrow']:
                day = res[day_key]
                if not day or not day['dateId']: continue
                
                cache_key = f"sched:{uid}:{day['dateId']}"
                cached_val = redis.get(cache_key)
                
                # Если график отличается и это не первая запись в базу
                if cached_val is not None and day['schedule'] != cached_val:
                    changed = True
                
                # Всегда обновляем базу актуальным значением
                redis.set(cache_key, day['schedule'], ex=172800)

            # 2. Если есть изменения, скидываем оба графика сразу
            if changed:
                msg = "🔔 <b>ОБНОВЛЕНИЕ ГРАФИКОВ!</b>\n\n"
                
                # Формируем блок "Сегодня"
                t = res['today']
                msg += f"📅 <b>Сегодня ({t['dateText']}):</b>\n{t['schedule']}\n\n"
                
                # Формируем блок "Завтра"
                tm = res['tomorrow']
                if tm and "не найден" not in tm['schedule'].lower():
                    msg += f"📅 <b>Завтра ({tm['dateText']}):</b>\n{tm['schedule']}\n\n"
                else:
                    msg += "📅 <b>Завтра:</b> Пока не опубликовано.\n\n"
                
                msg += f"🕒 <i>Данные на: {res['today']['updateTime']}</i>"
                
                try:
                    await bot.send_message(int(uid), msg, parse_mode="HTML")
                except Exception as e:
                    logging.error(f"Не смог отправить уведомление {uid}: {e}")
                    
# --- ИНТЕРФЕЙС ---
def get_kb(uid):
    is_mon = redis.sismember("monitoring_users", str(uid))
    return types.ReplyKeyboardMarkup(keyboard=[
        [types.KeyboardButton(text="Сегодня 💡"), types.KeyboardButton(text="Завтра 📅")],
        [types.KeyboardButton(text="Выключить мониторинг ❌" if is_mon else "Включить мониторинг 📡")]
    ], resize_keyboard=True)

@dp.message(Command("start"))
async def start(m: types.Message):
    await m.answer("Бот активен. Использую Upstash для памяти.", reply_markup=get_kb(m.from_user.id))

@dp.message(F.text.contains("мониторинг"))
async def toggle(m: types.Message):
    uid = str(m.from_user.id)
    if redis.sismember("monitoring_users", uid):
        redis.srem("monitoring_users", uid)
        await m.answer("📡 Мониторинг выключен.", reply_markup=get_kb(uid))
    else:
        redis.sadd("monitoring_users", uid)
        await m.answer("📡 Мониторинг включен!\n\nЯ запомнил текущий график. Если через 5 минут он изменится — я пришлю уведомление", reply_markup=get_kb(uid))

@dp.message(F.text.in_(["Сегодня 💡", "Завтра 📅"]))
async def manual(m: types.Message):
    s = await m.answer("🔍 Проверяю...")
    res = await get_dtek_full_data()
    if res:
        d = res['tomorrow'] if "Завтра" in m.text else res['today']
        if not d:
            await bot.edit_message_text("График на завтра еще не опубликован.", m.chat.id, s.message_id)
            return
        txt = f"<b>📅 {d['dateText']}</b>\n\n{d['schedule']}\n\n🕒 <i>Обновлено: {d['updateTime']}</i>"
        await bot.edit_message_text(text=txt, chat_id=m.chat.id, message_id=s.message_id, parse_mode="HTML")

async def main():
    asyncio.create_task(monitoring_task())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
