import os
import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import aiohttp
from telegram import Bot
from telegram.error import TelegramError
import pytz

# Конфигурация с твоите токени
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '8354673661:AAGaSRxyHa2WGFkyMjoTWg5qrC2Lxcf7s6M')
TELEGRAM_CHANNEL_ID = os.getenv('TELEGRAM_CHANNEL_ID', '-1003114970901')
API_FOOTBALL_KEY = os.getenv('API_FOOTBALL_KEY', '2589b526b382f3528eb485c95eac5080')

# Настройки за мартингейл
INITIAL_BET = 1.0  # 1 евро
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
        today = datetime.now().strftime('%Y-%m-%d')
        url = f"{self.BASE_URL}/fixtures"
        params = {
            'date': today,
            'status': 'NS'  # Not Started
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get('response', [])
                logger.error(f"API грешка: {response.status}")
                return []
    
    async def get_predictions(self, fixture_id: int) -> Optional[Dict]:
        """Взима предвиждания за конкретен мач"""
        url = f"{self.BASE_URL}/predictions"
        params = {'fixture': fixture_id}
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    results = data.get('response', [])
                    return results[0] if results else None
                return None
    
    async def get_odds(self, fixture_id: int) -> Optional[Dict]:
        """Взима коефициенти за мач"""
        url = f"{self.BASE_URL}/odds"
        params = {
            'fixture': fixture_id,
            'bookmaker': 8  # Bet365 ID
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    results = data.get('response', [])
                    return results[0] if results else None
                return None

class BettingStrategy:
    def __init__(self):
        self.current_bet = INITIAL_BET
        self.bets_today = []
        self.bet_results = []  # Резултати от предишни залози
    
    def calculate_next_bet(self, won: bool) -> float:
        """Изчислява следващ залог според мартингейл"""
        if won:
            self.current_bet = INITIAL_BET
        else:
            # Мартингейл: удвояваме залога след загуба
            self.current_bet = round(self.current_bet * 2.2, 2)
        return self.current_bet
    
    def add_bet_result(self, won: bool):
        """Добавя резултат от залог"""
        self.bet_results.append(won)
        self.calculate_next_bet(won)
    
    def reset_daily(self):
        """Нулира дневните статистики"""
        self.current_bet = INITIAL_BET
        self.bets_today = []
        self.bet_results = []

class BetSelector:
    def __init__(self, api: FootballAPI):
        self.api = api
    
    async def find_best_combination(self, excluded_fixture_ids: List[int] = None) -> Optional[Dict]:
        """Намира най-добрата комбинация с коефициент между 2.0-2.5 и най-висока вероятност"""
        if excluded_fixture_ids is None:
            excluded_fixture_ids = []
        
        logger.info("Започва търсене на подходящи мачове...")
        fixtures = await self.api.get_today_fixtures()
        logger.info(f"Намерени {len(fixtures)} мача за днес")
        
        # Филтрираме мачове, които са след 2 часа от сега
        now = datetime.now(pytz.UTC)
        future_fixtures = []
        
        for fixture in fixtures:
            try:
                fixture_time = datetime.fromisoformat(fixture['fixture']['date'].replace('Z', '+00:00'))
                if fixture_time > now + timedelta(hours=2) and fixture['fixture']['id'] not in excluded_fixture_ids:
                    future_fixtures.append(fixture)
            except Exception as e:
                logger.error(f"Грешка при обработка на време: {e}")
                continue
        
        logger.info(f"Филтрирани {len(future_fixtures)} подходящи мача")
        
        if not future_fixtures:
            logger.warning("Няма намерени подходящи мачове")
            return None
        
        # Анализираме всички мачове и събираме данни
        analyzed_bets = []
        
        for fixture in future_fixtures[:30]:  # Проверяваме до 30 мача
            try:
                fixture_id = fixture['fixture']['id']
                logger.info(f"Анализ на мач ID: {fixture_id}")
                
                prediction = await self.api.get_predictions(fixture_id)
                await asyncio.sleep(1)  # Rate limiting
                
                odds_data = await self.api.get_odds(fixture_id)
                await asyncio.sleep(1)  # Rate limiting
                
                if not prediction or not odds_data:
                    logger.info(f"Няма данни за мач {fixture_id}")
                    continue
                
                # Извличаме всички възможни залози с висока вероятност
                bet_options = self._extract_all_bet_options(prediction, odds_data, fixture)
                analyzed_bets.extend(bet_options)
                
            except Exception as e:
                logger.error(f"Грешка при анализ на мач {fixture['fixture']['id']}: {e}")
                continue
        
        logger.info(f"Анализирани {len(analyzed_bets)} възможни залога")
        
        if not analyzed_bets:
            logger.warning("Няма намерени подходящи залози")
            return None
        
        # Търсим най-добрата комбинация
        best_combination = self._find_optimal_combination(analyzed_bets)
        
        if best_combination:
            logger.info(f"Намерена комбинация с коефициент {best_combination['total_odd']:.2f} и вероятност {best_combination['avg_confidence']:.1f}%")
        
        return best_combination
    
    def _extract_all_bet_options(self, prediction: Dict, odds_data: Dict, fixture: Dict) -> List[Dict]:
        """Извлича всички възможни залози с високи проценти на успех"""
        options = []
        
        try:
            predictions = prediction.get('predictions', {})
            win_percent = predictions.get('percent', {})
            
            # Вземаме коефициентите
            bookmaker = odds_data.get('bookmakers', [{}])[0]
            bets = bookmaker.get('bets', [])
            
            # Намираме Match Winner залозите
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
            
            # Home Win
            home_percent = float(win_percent.get('home', '0').rstrip('%'))
            home_odd = float(values[0]['odd'])
            if home_percent > 40:  # Минимум 40% шанс
                options.append({
                    'type': f"🏠 {fixture['teams']['home']['name']} печели",
                    'odd': home_odd,
                    'confidence': home_percent,
                    'fixture': fixture,
                    'fixture_id': fixture['fixture']['id']
                })
            
            # Draw
            draw_percent = float(win_percent.get('draw', '0').rstrip('%'))
            draw_odd = float(values[1]['odd'])
            if draw_percent > 25:  # Минимум 25% шанс за равен
                options.append({
                    'type': f"🤝 Равен",
                    'odd': draw_odd,
                    'confidence': draw_percent,
                    'fixture': fixture,
                    'fixture_id': fixture['fixture']['id']
                })
            
            # Away Win
            away_percent = float(win_percent.get('away', '0').rstrip('%'))
            away_odd = float(values[2]['odd'])
            if away_percent > 40:  # Минимум 40% шанс
                options.append({
                    'type': f"✈️ {fixture['teams']['away']['name']} печели",
                    'odd': away_odd,
                    'confidence': away_percent,
                    'fixture': fixture,
                    'fixture_id': fixture['fixture']['id']
                })
            
        except Exception as e:
            logger.error(f"Грешка при извличане на опции: {e}")
        
        return options
    
    def _find_optimal_combination(self, bets: List[Dict]) -> Optional[Dict]:
        """Намира оптималната комбинация с коефициент 2.0-2.5 и най-висока вероятност"""
        if not bets:
            return None
        
        # Сортираме по вероятност
        bets.sort(key=lambda x: x['confidence'], reverse=True)
        
        best_combo = None
        best_score = 0  # Комбинирана оценка: вероятност * близост до цел
        
        # Проверка за 1 мач
        for bet in bets:
            if TARGET_ODD_MIN <= bet['odd'] <= TARGET_ODD_MAX:
                # Оценка: по-висока вероятност = по-добре
                score = bet['confidence']
                if score > best_score:
                    best_score = score
                    best_combo = {
                        'bets': [bet],
                        'total_odd': round(bet['odd'], 2),
                        'avg_confidence': bet['confidence']
                    }
        
        # Проверка за 2 мача
        for i in range(min(10, len(bets))):
            for j in range(i+1, min(10, len(bets))):
                # Не комбинираме мачове от същия fixture
                if bets[i]['fixture_id'] == bets[j]['fixture_id']:
                    continue
                
                combined_odd = bets[i]['odd'] * bets[j]['odd']
                if TARGET_ODD_MIN <= combined_odd <= TARGET_ODD_MAX:
                    avg_conf = (bets[i]['confidence'] + bets[j]['confidence']) / 2
                    # Бонус за по-близък коефициент до 2.2 (среден)
                    odd_bonus = 1 - abs(combined_odd - 2.2) / 0.5
                    score = avg_conf * odd_bonus
                    
                    if score > best_score:
                        best_score = score
                        best_combo = {
                            'bets': [bets[i], bets[j]],
                            'total_odd': round(combined_odd, 2),
                            'avg_confidence': avg_conf
                        }
        
        # Проверка за 3 мача
        for i in range(min(8, len(bets))):
            for j in range(i+1, min(8, len(bets))):
                for k in range(j+1, min(8, len(bets))):
                    # Проверка за уникални fixture_id
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
        """Изпраща нотификация за нов залог"""
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
                time_bg = time.astimezone(pytz.timezone('Europe/Sofia'))
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
            logger.info(f"✅ Изпратена нотификация за залог #{bet_number}")
        except TelegramError as e:
            logger.error(f"❌ Грешка при изпращане: {e}")
    
    async def send_daily_start(self):
        """Изпраща съобщение за начало на деня"""
        message = "🌅 <b>ДОБРО УТРО!</b>\n\n"
        message += "Започваме нов ден с нашата мартингейл стратегия!\n\n"
        message += "📅 Планирани залози: 5\n"
        message += "💰 Начална сума: 1.00 EUR\n"
        message += "📊 Целеви коефициент: 2.0 - 2.5\n\n"
        message += "🔥 Нека спечелим!"
        
        try:
            await self.bot.send_message(
                chat_id=self.channel_id,
                text=message,
                parse_mode='HTML'
            )
        except TelegramError as e:
            logger.error(f"Грешка при изпращане: {e}")

async def main_loop():
    """Основен цикъл на бота"""
    api = FootballAPI(API_FOOTBALL_KEY)
    selector = BetSelector(api)
    notifier = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID)
    strategy = BettingStrategy()
    
    logger.info("🚀 Ботът стартира успешно!")
    
    used_fixture_ids = []
    last_check_date = None
    daily_start_sent = False
    processed_times = set()
    
    while True:
        try:
            now = datetime.now(pytz.timezone('Europe/Sofia'))
            current_date = now.date()
            current_time = now.strftime('%H:%M')
            
            # Нулираме при нов ден
            if last_check_date != current_date:
                strategy.reset_daily()
                used_fixture_ids = []
                last_check_date = current_date
                daily_start_sent = False
                processed_times = set()
                logger.info(f"📅 Нов ден: {current_date}")
            
            # Изпращаме начално съобщение
            if not daily_start_sent and current_time >= "07:00":
                await notifier.send_daily_start()
                daily_start_sent = True
            
            # Проверяваме дали е време за залог
            if current_time in BET_TIMES and current_time not in processed_times:
                if len(strategy.bets_today) >= MAX_BETS_PER_DAY:
                    logger.info(f"⚠️ Достигнат лимит от {MAX_BETS_PER_DAY} залога за днес")
                    processed_times.add(current_time)
                else:
                    logger.info(f"🔍 Търсене на залог в {current_time}...")
                    
                    combination = await selector.find_best_combination(used_fixture_ids)
                    
                    if combination:
                        bet_number = len(strategy.bets_today) + 1
                        bet_amount = strategy.current_bet
                        
                        await notifier.send_bet_notification(combination, bet_amount, bet_number)
                        
                        # Запазваме информация за залога
                        strategy.bets_today.append({
                            'combination': combination,
                            'amount': bet_amount,
                            'time': now
                        })
                        
                        # Добавяме fixture ID-та към използваните
                        for bet in combination['bets']:
                            used_fixture_ids.append(bet['fixture_id'])
                        
                        logger.info(f"✅ Залог #{bet_number} публикуван успешно!")
                        processed_times.add(current_time)
                    else:
                        logger.warning("⚠️ Не е намерена подходяща комбинация в този момент")
                        # Не добавяме в processed_times, за да опитаме отново по-късно
            
            # Изчакваме 30 секунди
            await asyncio.sleep(30)
            
        except Exception as e:
            logger.error(f"❌ Грешка в главния цикъл: {e}")
            await asyncio.sleep(60)

if __name__ == '__main__':
    asyncio.run(main_loop())
