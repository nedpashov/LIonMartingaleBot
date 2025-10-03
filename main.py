import os
import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import aiohttp
from telegram import Bot
from telegram.error import TelegramError
import pytz
from aiohttp import web
import traceback

# Конфигурация с твоите токени
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '8354673661:AAGaSRxyHa2WGFkyMjoTWg5qrC2Lxcf7s6M')
TELEGRAM_CHANNEL_ID = os.getenv('TELEGRAM_CHANNEL_ID', '-1003114970901')
API_FOOTBALL_KEY = os.getenv('API_FOOTBALL_KEY', '2589b526b382f3528eb485c95eac5080')
PORT = int(os.getenv('PORT', 10000))

# Българска времева зона
BG_TZ = pytz.timezone('Europe/Sofia')

# Настройки за мартингейл
INITIAL_BET = 1.0
TARGET_ODD_MIN = 2.0
TARGET_ODD_MAX = 2.5
MAX_BETS_PER_DAY = 5
BET_TIMES = ['08:00', '11:00', '14:00', '17:00', '20:00']

# Логване
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class FootballAPI:
    BASE_URL = "https://v3.football.api-sports.io"
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {
            'x-apisports-key': api_key
        }
    
    async def get_today_fixtures(self) -> List[Dict]:
        """Взима мачовете за днес"""
        today = datetime.now(BG_TZ).strftime('%Y-%m-%d')
        url = f"{self.BASE_URL}/fixtures"
        params = {
            'date': today,
            'timezone': 'Europe/Sofia'
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.headers, params=params, timeout=aiohttp.ClientTimeout(total=30)) as response:
                    logger.info(f"API fixtures status: {response.status}")
                    if response.status == 200:
                        data = await response.json()
                        fixtures = data.get('response', [])
                        logger.info(f"Получени {len(fixtures)} мача от API")
                        return fixtures
                    else:
                        error_text = await response.text()
                        logger.error(f"API грешка {response.status}: {error_text}")
                        return []
        except Exception as e:
            logger.error(f"Exception при get_today_fixtures: {e}")
            logger.error(traceback.format_exc())
            return []
    
    async def get_predictions(self, fixture_id: int) -> Optional[Dict]:
        """Взима предвиждания за конкретен мач"""
        url = f"{self.BASE_URL}/predictions"
        params = {'fixture': fixture_id}
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.headers, params=params, timeout=aiohttp.ClientTimeout(total=30)) as response:
                    if response.status == 200:
                        data = await response.json()
                        results = data.get('response', [])
                        return results[0] if results else None
                    else:
                        logger.warning(f"Predictions API status {response.status} за мач {fixture_id}")
                        return None
        except Exception as e:
            logger.error(f"Exception при get_predictions: {e}")
            return None
    
    async def get_odds(self, fixture_id: int) -> Optional[Dict]:
        """Взима коефициенти за мач"""
        url = f"{self.BASE_URL}/odds"
        params = {
            'fixture': fixture_id,
            'bookmaker': 8
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.headers, params=params, timeout=aiohttp.ClientTimeout(total=30)) as response:
                    if response.status == 200:
                        data = await response.json()
                        results = data.get('response', [])
                        return results[0] if results else None
                    else:
                        logger.warning(f"Odds API status {response.status} за мач {fixture_id}")
                        return None
        except Exception as e:
            logger.error(f"Exception при get_odds: {e}")
            return None

class BettingStrategy:
    def __init__(self):
        self.current_bet = INITIAL_BET
        self.bets_today = []
        self.bet_results = []
    
    def calculate_next_bet(self, won: bool) -> float:
        if won:
            self.current_bet = INITIAL_BET
        else:
            self.current_bet = round(self.current_bet * 2.2, 2)
        return self.current_bet
    
    def add_bet_result(self, won: bool):
        self.bet_results.append(won)
        self.calculate_next_bet(won)
    
    def reset_daily(self):
        self.current_bet = INITIAL_BET
        self.bets_today = []
        self.bet_results = []

class BetSelector:
    def __init__(self, api: FootballAPI):
        self.api = api
    
    async def find_best_combination(self, excluded_fixture_ids: List[int] = None) -> Optional[Dict]:
        if excluded_fixture_ids is None:
            excluded_fixture_ids = []
        
        logger.info("🔍 Започва търсене на подходящи мачове...")
        
        try:
            fixtures = await self.api.get_today_fixtures()
            logger.info(f"📊 Намерени {len(fixtures)} мача за днес")
            
            if not fixtures:
                logger.warning("⚠️ API не върна мачове за днес!")
                return None
            
            now = datetime.now(BG_TZ)
            future_fixtures = []
            
            for fixture in fixtures:
                try:
                    fixture_status = fixture['fixture']['status']['short']
                    
                    if fixture_status not in ['NS', 'TBD']:
                        continue
                    
                    fixture_time_str = fixture['fixture']['date']
                    fixture_time = datetime.fromisoformat(fixture_time_str.replace('Z', '+00:00'))
                    fixture_time_bg = fixture_time.astimezone(BG_TZ)
                    
                    time_diff = (fixture_time_bg - now).total_seconds() / 3600
                    
                    if time_diff > 1 and fixture['fixture']['id'] not in excluded_fixture_ids:
                        future_fixtures.append(fixture)
                        logger.info(f"  ✅ Добавен: {fixture['teams']['home']['name']} vs {fixture['teams']['away']['name']} в {fixture_time_bg.strftime('%H:%M')}")
                        
                except Exception as e:
                    logger.error(f"❌ Грешка при филтриране: {e}")
                    continue
            
            logger.info(f"📋 Филтрирани {len(future_fixtures)} подходящи мача")
            
            if not future_fixtures:
                logger.warning("⚠️ Няма намерени бъдещи мачове!")
                return None
            
            analyzed_bets = []
            checked_count = 0
            max_to_check = min(20, len(future_fixtures))
            
            for fixture in future_fixtures[:max_to_check]:
                try:
                    fixture_id = fixture['fixture']['id']
                    home = fixture['teams']['home']['name']
                    away = fixture['teams']['away']['name']
                    
                    logger.info(f"🔎 Анализ #{checked_count+1}/{max_to_check}: {home} vs {away}")
                    checked_count += 1
                    
                    prediction = await self.api.get_predictions(fixture_id)
                    await asyncio.sleep(1.5)
                    
                    if not prediction:
                        logger.info(f"  ⚠️ Няма predictions")
                        continue
                    
                    odds_data = await self.api.get_odds(fixture_id)
                    await asyncio.sleep(1.5)
                    
                    if not odds_data:
                        logger.info(f"  ⚠️ Няма odds")
                        continue
                    
                    bet_options = self._extract_all_bet_options(prediction, odds_data, fixture)
                    if bet_options:
                        analyzed_bets.extend(bet_options)
                        logger.info(f"  ✅ Намерени {len(bet_options)} опции")
                    
                except Exception as e:
                    logger.error(f"❌ Exception при анализ: {e}")
                    logger.error(traceback.format_exc())
                    continue
            
            logger.info(f"📈 Общо {len(analyzed_bets)} възможни залога")
            
            if not analyzed_bets:
                logger.warning("⚠️ Няма намерени залози")
                return None
            
            best_combination = self._find_optimal_combination(analyzed_bets)
            
            if best_combination:
                logger.info(f"🎯 Намерена комбинация: {best_combination['total_odd']:.2f}, {best_combination['avg_confidence']:.1f}%")
            
            return best_combination
            
        except Exception as e:
            logger.error(f"❌ CRITICAL Exception в find_best_combination: {e}")
            logger.error(traceback.format_exc())
            return None
    
    def _extract_all_bet_options(self, prediction: Dict, odds_data: Dict, fixture: Dict) -> List[Dict]:
        options = []
        
        try:
            predictions = prediction.get('predictions', {})
            win_percent = predictions.get('percent', {})
            
            bookmaker = odds_data.get('bookmakers', [{}])[0]
            bets = bookmaker.get('bets', [])
            
            match_winner = None
            for bet in bets:
                if bet['name'] == 'Match Winner':
                    match_winner = bet
                    break
            
            if not match_winner:
                return options
            
            values = match_winner.get('values', [])
            if len(values) < 3:
                return options
            
            home_percent = float(win_percent.get('home', '0').rstrip('%'))
            home_odd = float(values[0]['odd'])
            if home_percent >= 35:
                options.append({
                    'type': f"🏠 {fixture['teams']['home']['name']} печели",
                    'odd': home_odd,
                    'confidence': home_percent,
                    'fixture': fixture,
                    'fixture_id': fixture['fixture']['id']
                })
            
            draw_percent = float(win_percent.get('draw', '0').rstrip('%'))
            draw_odd = float(values[1]['odd'])
            if draw_percent >= 20:
                options.append({
                    'type': f"🤝 Равен",
                    'odd': draw_odd,
                    'confidence': draw_percent,
                    'fixture': fixture,
                    'fixture_id': fixture['fixture']['id']
                })
            
            away_percent = float(win_percent.get('away', '0').rstrip('%'))
            away_odd = float(values[2]['odd'])
            if away_percent >= 35:
                options.append({
                    'type': f"✈️ {fixture['teams']['away']['name']} печели",
                    'odd': away_odd,
                    'confidence': away_percent,
                    'fixture': fixture,
                    'fixture_id': fixture['fixture']['id']
                })
            
        except Exception as e:
            logger.error(f"Exception при extract: {e}")
        
        return options
    
    def _find_optimal_combination(self, bets: List[Dict]) -> Optional[Dict]:
        if not bets:
            return None
        
        bets.sort(key=lambda x: x['confidence'], reverse=True)
        
        best_combo = None
        best_score = 0
        
        for bet in bets:
            if TARGET_ODD_MIN <= bet['odd'] <= TARGET_ODD_MAX:
                score = bet['confidence']
                if score > best_score:
                    best_score = score
                    best_combo = {
                        'bets': [bet],
                        'total_odd': round(bet['odd'], 2),
                        'avg_confidence': bet['confidence']
                    }
        
        for i in range(min(15, len(bets))):
            for j in range(i+1, min(15, len(bets))):
                if bets[i]['fixture_id'] == bets[j]['fixture_id']:
                    continue
                
                combined_odd = bets[i]['odd'] * bets[j]['odd']
                if TARGET_ODD_MIN <= combined_odd <= TARGET_ODD_MAX:
                    avg_conf = (bets[i]['confidence'] + bets[j]['confidence']) / 2
                    odd_bonus = 1 - abs(combined_odd - 2.2) / 0.5
                    score = avg_conf * odd_bonus
                    
                    if score > best_score:
                        best_score = score
                        best_combo = {
                            'bets': [bets[i], bets[j]],
                            'total_odd': round(combined_odd, 2),
                            'avg_confidence': avg_conf
                        }
        
        for i in range(min(10, len(bets))):
            for j in range(i+1, min(10, len(bets))):
                for k in range(j+1, min(10, len(bets))):
                    ids = {bets[i]['fixture_id'], bets[j]['fixture_id'], bets[k]['fixture_id']}
                    if len(ids) < 3:
                        continue
                    
                    combined_odd = bets[i]['odd'] * bets[j]['odd'] * bets[k]['odd']
                    if TARGET_ODD_MIN <= combined_odd <= TARGET_ODD_MAX:
                        avg_conf = (bets[i]['confidence'] + bets[j]['confidence'] + bets[k]['confidence']) / 3
                        odd_bonus = 1 - abs(combined_odd - 2.2) / 0.5
                        score = avg_conf * odd_bonus
                        
                        if score > best_score:
                            best_score = score
                            best_combo = {
                                'bets': [bets[i], bets[j], bets[k]],
                                'total_odd': round(combined_odd, 2),
                                'avg_confidence': avg_conf
                            }
        
        return best_combo

class TelegramNotifier:
    def __init__(self, token: str, channel_id: str):
        self.bot = Bot(token=token)
        self.channel_id = channel_id
    
    async def send_bet_notification(self, combination: Dict, bet_amount: float, bet_number: int):
        message = f"🎯 <b>ЗАЛОГ #{bet_number} ЗА ДНЕС</b>\n\n"
        message += f"💰 <b>Сума:</b> {bet_amount:.2f} EUR\n"
        message += f"📊 <b>Общ коефициент:</b> {combination['total_odd']:.2f}\n"
        message += f"✅ <b>Вероятност за успех:</b> {combination['avg_confidence']:.1f}%\n"
        message += f"💵 <b>Потенциална печалба:</b> {bet_amount * combination['total_odd']:.2f} EUR\n\n"
        
        message += "<b>═══════════════════</b>\n"
        message += "<b>КОМБИНАЦИЯ:</b>\n\n"
        
        for idx, bet in enumerate(combination['bets'], 1):
            fixture = bet['fixture']
            home = fixture['teams']['home']['name']
            away = fixture['teams']['away']['name']
            league = fixture['league']['name']
            
            try:
                time = datetime.fromisoformat(fixture['fixture']['date'].replace('Z', '+00:00'))
                time_bg = time.astimezone(BG_TZ)
                time_str = time_bg.strftime('%H:%M')
            except:
                time_str = "TBA"
            
            message += f"<b>{idx}. {home} vs {away}</b>\n"
            message += f"   🏆 {league}\n"
            message += f"   🕐 Час: {time_str}\n"
            message += f"   🎲 Залог: {bet['type']}\n"
            message += f"   📈 Коефициент: {bet['odd']:.2f}\n"
            message += f"   ✅ Вероятност: {bet['confidence']:.1f}%\n\n"
        
        message += "<b>═══════════════════</b>\n"
        message += "🔔 Следете резултатите и чакайте следващия залог!\n"
        message += "💪 Успех!"
        
        try:
            await self.bot.send_message(
                chat_id=self.channel_id,
                text=message,
                parse_mode='HTML'
            )
            logger.info(f"✅ Telegram: Изпратена нотификация #{bet_number}")
        except TelegramError as e:
            logger.error(f"❌ Telegram грешка: {e}")
    
    async def send_daily_start(self):
        now = datetime.now(BG_TZ)
        message = f"🌅 <b>ДОБРО УТРО!</b>\n\n"
        message += f"📅 Дата: {now.strftime('%d.%m.%Y')}\n"
        message += f"🕐 Време: {now.strftime('%H:%M')}\n\n"
        message += "Започваме нов ден с нашата мартингейл стратегия!\n\n"
        message += "📋 Планирани залози: 5\n"
        message += "💰 Начална сума: 1.00 EUR\n"
        message += "📊 Целеви коефициент: 2.0 - 2.5\n"
        message += "🕐 Часове: 08:00, 11:00, 14:00, 17:00, 20:00\n\n"
        message += "🔥 Нека спечелим!"
        
        try:
            await self.bot.send_message(
                chat_id=self.channel_id,
                text=message,
                parse_mode='HTML'
            )
            logger.info("✅ Telegram: Изпратено добро утро")
        except TelegramError as e:
            logger.error(f"❌ Telegram грешка: {e}")
    
    async def send_debug(self, text: str):
        try:
            await self.bot.send_message(
                chat_id=self.channel_id,
                text=f"🔧 {text}",
                parse_mode='HTML'
            )
        except:
            pass

# Web server
async def health_check(request):
    now = datetime.now(BG_TZ)
    return web.Response(text=f"Bot is running! 🚀\nBG Time: {now.strftime('%H:%M:%S')}")

async def status(request):
    now = datetime.now(BG_TZ)
    return web.json_response({
        "status": "active",
        "bg_time": now.strftime('%Y-%m-%d %H:%M:%S'),
        "next_bets": BET_TIMES
    })

async def test_bet(request):
    """Ръчен тест за залог - достъпен на /test"""
    try:
        api = FootballAPI(API_FOOTBALL_KEY)
        selector = BetSelector(api)
        notifier = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID)
        
        logger.info("🧪 Тестов залог - търсене започва...")
        await notifier.send_debug("🧪 Тестово търсене на залог...")
        
        combination = await selector.find_best_combination([])
        
        if combination:
            await notifier.send_bet_notification(combination, 1.0, 999)
            return web.json_response({
                "success": True,
                "message": "Намерена комбинация!",
                "odd": combination['total_odd'],
                "confidence": combination['avg_confidence']
            })
        else:
            await notifier.send_debug("⚠️ Няма намерена комбинация при тест")
            return web.json_response({
                "success": False,
                "message": "Няма намерена подходяща комбинация"
            })
    
    except Exception as e:
        logger.error(f"Грешка при тест: {e}")
        logger.error(traceback.format_exc())
        return web.json_response({
            "success": False,
            "error": str(e)
        })

# Self-ping за keepalive
async def keep_alive():
    """Ping-ва себе си на всеки 10 минути за да остане активен"""
    while True:
        try:
            await asyncio.sleep(600)  # 10 минути
            async with aiohttp.ClientSession() as session:
                async with session.get('http://localhost:10000/') as resp:
                    logger.info(f"🔄 Keepalive ping: {resp.status}")
        except Exception as e:
            logger.error(f"Keepalive грешка: {e}")

async def bot_loop():
    """Основен цикъл"""
    api = FootballAPI(API_FOOTBALL_KEY)
    selector = BetSelector(api)
    notifier = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID)
    strategy = BettingStrategy()
    
    logger.info("🚀 BOT LOOP ЗАПОЧВА!")
    
    try:
        await notifier.send_debug(f"Бот стартира! {datetime.now(BG_TZ).strftime('%H:%M')}")
    except Exception as e:
        logger.error(f"Грешка при старт съобщение: {e}")
    
    used_fixture_ids = []
    last_check_date = None
    daily_start_sent = False
    processed_times = set()
    
    loop_count = 0
    
    while True:
        try:
            loop_count += 1
            now = datetime.now(BG_TZ)
            current_date = now.date()
            current_time = now.strftime('%H:%M')
            
            # Логваме на всяка минута
            if loop_count % 2 == 0:  # На всеки 2 цикъла = 1 минута
                logger.info(f"⏰ BG Време: {current_time} | Дата: {current_date} | Loop: {loop_count}")
            
            # Нов ден
            if last_check_date != current_date:
                strategy.reset_daily()
                used_fixture_ids = []
                last_check_date = current_date
                daily_start_sent = False
                processed_times = set()
                logger.info(f"📅 НОВ ДЕН: {current_date}")
            
            # Добро утро
            if not daily_start_sent and current_time >= "07:00" and current_time < "08:00":
                logger.info("🌅 Изпращам добро утро...")
                try:
                    await notifier.send_daily_start()
                    daily_start_sent = True
                except Exception as e:
                    logger.error(f"Грешка при добро утро: {e}")
            
            # Време за залог
            if current_time in BET_TIMES and current_time not in processed_times:
                if len(strategy.bets_today) >= MAX_BETS_PER_DAY:
                    logger.info(f"⚠️ Достигнат лимит {MAX_BETS_PER_DAY} залога")
                    processed_times.add(current_time)
                else:
                    logger.info(f"🎯 ВРЕМЕ ЗА ЗАЛОГ: {current_time}")
                    
                    try:
                        combination = await selector.find_best_combination(used_fixture_ids)
                        
                        if combination:
                            bet_number = len(strategy.bets_today) + 1
                            bet_amount = strategy.current_bet
                            
                            await notifier.send_bet_notification(combination, bet_amount, bet_number)
                            
                            strategy.bets_today.append({
                                'combination': combination,
                                'amount': bet_amount,
                                'time': now
                            })
                            
                            for bet in combination['bets']:
                                used_fixture_ids.append(bet['fixture_id'])
                            
                            logger.info(f"🎉 Залог #{bet_number} публикуван!")
                            processed_times.add(current_time)
                        else:
                            logger.warning(f"⚠️ Няма комбинация в {current_time}")
                            await notifier.send_debug(f"Няма подходящи мачове в {current_time}")
                    
                    except Exception as e:
                        logger.error(f"❌ EXCEPTION при търсене на залог: {e}")
                        logger.error(traceback.format_exc())
            
            await asyncio.sleep(30)
            
        except Exception as e:
            logger.error(f"❌ CRITICAL ERROR в bot_loop: {e}")
            logger.error(traceback.format_exc())
            await asyncio.sleep(60)

async def start_background_tasks(app):
    app['bot_task'] = asyncio.create_task(bot_loop())
    app['keepalive_task'] = asyncio.create_task(keep_alive())

async def cleanup_background_tasks(app):
    app['bot_task'].cancel()
    app['keepalive_task'].cancel()
    try:
        await app['bot_task']
        await app['keepalive_task']
    except:
        pass

if __name__ == '__main__':
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/status', status)
    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)
    
    logger.info(f"🌐 Web server на порт {PORT}")
    web.run_app(app, host='0.0.0.0', port=PORT)
