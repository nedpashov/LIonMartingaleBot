import os
import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
import aiohttp
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.error import TelegramError
import pytz
from aiohttp import web
import traceback
import sqlite3
import json

# Конфигурация
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '8354673661:AAGaSRxyHa2WGFkyMjoTWg5qrC2Lxcf7s6M')
TELEGRAM_CHANNEL_ID = os.getenv('TELEGRAM_CHANNEL_ID', '-1003114970901')
API_FOOTBALL_KEY = os.getenv('API_FOOTBALL_KEY', '2589b526b382f3528eb485c95eac5080')
PORT = int(os.getenv('PORT', 10000))

BG_TZ = pytz.timezone('Europe/Sofia')

# Настройки
INITIAL_BET = 1.0
TARGET_ODD_MIN = 2.0
TARGET_ODD_MAX = 2.5
MAX_BETS_PER_DAY = 8  # Увеличихме от 5 на 8
MARTINGALE_MULTIPLIER = 2.2

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Database Manager
class DatabaseManager:
    def __init__(self, db_path='bets.db'):
        self.db_path = db_path
        self.init_db()
    
    def init_db(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        # Таблица за залози
        c.execute('''CREATE TABLE IF NOT EXISTS bets
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      bet_number INTEGER,
                      date TEXT,
                      amount REAL,
                      odd REAL,
                      potential_win REAL,
                      bet_type TEXT,
                      fixtures TEXT,
                      status TEXT,
                      result TEXT,
                      profit REAL,
                      timestamp TEXT)''')
        
        # Таблица за статистики
        c.execute('''CREATE TABLE IF NOT EXISTS daily_stats
                     (date TEXT PRIMARY KEY,
                      total_bets INTEGER,
                      won_bets INTEGER,
                      lost_bets INTEGER,
                      pending_bets INTEGER,
                      total_staked REAL,
                      total_profit REAL,
                      success_rate REAL)''')
        
        conn.commit()
        conn.close()
        logger.info("Database initialized")
    
    def save_bet(self, bet_data: Dict):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        c.execute('''INSERT INTO bets 
                     (bet_number, date, amount, odd, potential_win, bet_type, 
                      fixtures, status, timestamp)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                  (bet_data['bet_number'], bet_data['date'], bet_data['amount'],
                   bet_data['odd'], bet_data['potential_win'], bet_data['bet_type'],
                   json.dumps(bet_data['fixtures']), 'pending',
                   datetime.now(BG_TZ).isoformat()))
        
        conn.commit()
        conn.close()
    
    def update_bet_result(self, bet_id: int, result: str, profit: float):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        c.execute('''UPDATE bets SET result = ?, profit = ?, status = 'completed'
                     WHERE id = ?''', (result, profit, bet_id))
        
        conn.commit()
        conn.close()
    
    def get_pending_bets(self) -> List[Dict]:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        c.execute("SELECT * FROM bets WHERE status = 'pending'")
        rows = c.fetchall()
        
        bets = []
        for row in rows:
            bets.append({
                'id': row[0],
                'bet_number': row[1],
                'fixtures': json.loads(row[7]),
                'amount': row[3],
                'odd': row[4]
            })
        
        conn.close()
        return bets
    
    def get_daily_stats(self, date: str) -> Dict:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        c.execute("SELECT * FROM bets WHERE date = ?", (date,))
        bets = c.fetchall()
        
        total_bets = len(bets)
        won_bets = sum(1 for b in bets if b[9] == 'won')
        lost_bets = sum(1 for b in bets if b[9] == 'lost')
        pending_bets = sum(1 for b in bets if b[8] == 'pending')
        
        total_staked = sum(b[3] for b in bets)
        total_profit = sum(b[10] if b[10] else 0 for b in bets)
        
        success_rate = (won_bets / total_bets * 100) if total_bets > 0 else 0
        
        conn.close()
        
        return {
            'total_bets': total_bets,
            'won_bets': won_bets,
            'lost_bets': lost_bets,
            'pending_bets': pending_bets,
            'total_staked': total_staked,
            'total_profit': total_profit,
            'success_rate': success_rate
        }

class FootballAPI:
    BASE_URL = "https://v3.football.api-sports.io"
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {'x-apisports-key': api_key}
    
    async def get_live_fixtures(self) -> List[Dict]:
        """Взима всички налични мачове (днес + утре)"""
        fixtures = []
        
        for days_offset in [0, 1]:
            date = (datetime.now(BG_TZ) + timedelta(days=days_offset)).strftime('%Y-%m-%d')
            url = f"{self.BASE_URL}/fixtures"
            params = {'date': date, 'timezone': 'Europe/Sofia'}
            
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers=self.headers, params=params, 
                                          timeout=aiohttp.ClientTimeout(total=30)) as response:
                        if response.status == 200:
                            data = await response.json()
                            fixtures.extend(data.get('response', []))
            except Exception as e:
                logger.error(f"Error getting fixtures: {e}")
        
        return fixtures
    
    async def get_predictions(self, fixture_id: int) -> Optional[Dict]:
        url = f"{self.BASE_URL}/predictions"
        params = {'fixture': fixture_id}
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.headers, params=params,
                                      timeout=aiohttp.ClientTimeout(total=30)) as response:
                    if response.status == 200:
                        data = await response.json()
                        results = data.get('response', [])
                        return results[0] if results else None
        except Exception as e:
            logger.error(f"Error getting predictions: {e}")
        return None
    
    async def get_odds(self, fixture_id: int) -> Optional[Dict]:
        url = f"{self.BASE_URL}/odds"
        params = {'fixture': fixture_id, 'bookmaker': 8}
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.headers, params=params,
                                      timeout=aiohttp.ClientTimeout(total=30)) as response:
                    if response.status == 200:
                        data = await response.json()
                        results = data.get('response', [])
                        return results[0] if results else None
        except Exception as e:
            logger.error(f"Error getting odds: {e}")
        return None
    
    async def get_fixture_result(self, fixture_id: int) -> Optional[Dict]:
        """Проверява резултат на завършен мач"""
        url = f"{self.BASE_URL}/fixtures"
        params = {'id': fixture_id}
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.headers, params=params,
                                      timeout=aiohttp.ClientTimeout(total=30)) as response:
                    if response.status == 200:
                        data = await response.json()
                        fixtures = data.get('response', [])
                        if fixtures:
                            return fixtures[0]
        except Exception as e:
            logger.error(f"Error getting result: {e}")
        return None

class BettingStrategy:
    def __init__(self, db: DatabaseManager):
        self.db = db
        self.current_bet = INITIAL_BET
        self.bets_today = []
        self.last_result = None
    
    def calculate_next_bet(self, won: bool) -> float:
        if won:
            self.current_bet = INITIAL_BET
        else:
            self.current_bet = round(self.current_bet * MARTINGALE_MULTIPLIER, 2)
        return self.current_bet
    
    def reset_daily(self):
        self.current_bet = INITIAL_BET
        self.bets_today = []
        self.last_result = None

class AdvancedBetSelector:
    def __init__(self, api: FootballAPI):
        self.api = api
    
    async def find_smart_combination(self, excluded_ids: List[int] = None) -> Optional[Dict]:
        if excluded_ids is None:
            excluded_ids = []
        
        logger.info("Smart search starting...")
        fixtures = await self.api.get_live_fixtures()
        logger.info(f"Found {len(fixtures)} total fixtures")
        
        now = datetime.now(BG_TZ)
        future_fixtures = []
        
        for fixture in fixtures:
            try:
                status = fixture['fixture']['status']['short']
                if status not in ['NS', 'TBD']:
                    continue
                
                fixture_time = datetime.fromisoformat(
                    fixture['fixture']['date'].replace('Z', '+00:00')
                ).astimezone(BG_TZ)
                
                hours_until = (fixture_time - now).total_seconds() / 3600
                
                if 1 < hours_until < 24 and fixture['fixture']['id'] not in excluded_ids:
                    future_fixtures.append(fixture)
            except:
                continue
        
        logger.info(f"Filtered {len(future_fixtures)} upcoming fixtures")
        
        if not future_fixtures:
            return None
        
        all_bet_options = []
        
        for fixture in future_fixtures[:30]:
            try:
                fixture_id = fixture['fixture']['id']
                
                prediction = await self.api.get_predictions(fixture_id)
                await asyncio.sleep(1.5)
                
                if not prediction:
                    continue
                
                odds_data = await self.api.get_odds(fixture_id)
                await asyncio.sleep(1.5)
                
                if not odds_data:
                    continue
                
                options = self._extract_all_bet_types(prediction, odds_data, fixture)
                all_bet_options.extend(options)
                
            except Exception as e:
                logger.error(f"Analysis error: {e}")
                continue
        
        logger.info(f"Total {len(all_bet_options)} bet options found")
        
        if not all_bet_options:
            return None
        
        return self._find_best_combination(all_bet_options)
    
    def _extract_all_bet_types(self, prediction: Dict, odds_data: Dict, 
                               fixture: Dict) -> List[Dict]:
        options = []
        
        try:
            predictions = prediction.get('predictions', {})
            win_percent = predictions.get('percent', {})
            
            bookmaker = odds_data.get('bookmakers', [{}])[0]
            bets = bookmaker.get('bets', [])
            
            # Match Winner
            for bet in bets:
                if bet['name'] == 'Match Winner':
                    values = bet.get('values', [])
                    if len(values) >= 3:
                        # Home
                        home_pct = float(win_percent.get('home', '0').rstrip('%'))
                        if home_pct >= 35:
                            options.append({
                                'type': f"🏠 {fixture['teams']['home']['name']} wins",
                                'bet_category': 'Match Winner',
                                'odd': float(values[0]['odd']),
                                'confidence': home_pct,
                                'fixture': fixture,
                                'fixture_id': fixture['fixture']['id'],
                                'prediction_key': 'home'
                            })
                        
                        # Draw
                        draw_pct = float(win_percent.get('draw', '0').rstrip('%'))
                        if draw_pct >= 20:
                            options.append({
                                'type': f"🤝 Draw",
                                'bet_category': 'Match Winner',
                                'odd': float(values[1]['odd']),
                                'confidence': draw_pct,
                                'fixture': fixture,
                                'fixture_id': fixture['fixture']['id'],
                                'prediction_key': 'draw'
                            })
                        
                        # Away
                        away_pct = float(win_percent.get('away', '0').rstrip('%'))
                        if away_pct >= 35:
                            options.append({
                                'type': f"✈️ {fixture['teams']['away']['name']} wins",
                                'bet_category': 'Match Winner',
                                'odd': float(values[2]['odd']),
                                'confidence': away_pct,
                                'fixture': fixture,
                                'fixture_id': fixture['fixture']['id'],
                                'prediction_key': 'away'
                            })
                
                # Over/Under Goals
                elif bet['name'] == 'Goals Over/Under':
                    values = bet.get('values', [])
                    goals_pred = predictions.get('goals', {})
                    
                    for val in values:
                        if 'Over' in val['value']:
                            over_pct = 50  # Default, API doesn't give exact %
                            if over_pct >= 40:
                                options.append({
                                    'type': f"⚽ {val['value']}",
                                    'bet_category': 'Over/Under',
                                    'odd': float(val['odd']),
                                    'confidence': over_pct,
                                    'fixture': fixture,
                                    'fixture_id': fixture['fixture']['id'],
                                    'prediction_key': val['value']
                                })
                
                # Both Teams Score
                elif bet['name'] == 'Both Teams Score':
                    values = bet.get('values', [])
                    btts = predictions.get('comparison', {}).get('att', {})
                    
                    if len(values) >= 1:
                        btts_yes_pct = 45  # Default estimate
                        if btts_yes_pct >= 35:
                            options.append({
                                'type': f"🎯 Both Teams Score - Yes",
                                'bet_category': 'BTTS',
                                'odd': float(values[0]['odd']),
                                'confidence': btts_yes_pct,
                                'fixture': fixture,
                                'fixture_id': fixture['fixture']['id'],
                                'prediction_key': 'btts_yes'
                            })
            
        except Exception as e:
            logger.error(f"Extract error: {e}")
        
        return options
    
    def _find_best_combination(self, bets: List[Dict]) -> Optional[Dict]:
        if not bets:
            return None
        
        bets.sort(key=lambda x: x['confidence'], reverse=True)
        
        best_combo = None
        best_score = 0
        
        # Single bet
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
        
        # Double
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
        
        # Triple
        for i in range(min(10, len(bets))):
            for j in range(i+1, min(10, len(bets))):
                for k in range(j+1, min(10, len(bets))):
                    ids = {bets[i]['fixture_id'], bets[j]['fixture_id'], 
                          bets[k]['fixture_id']}
                    if len(ids) < 3:
                        continue
                    
                    combined_odd = bets[i]['odd'] * bets[j]['odd'] * bets[k]['odd']
                    if TARGET_ODD_MIN <= combined_odd <= TARGET_ODD_MAX:
                        avg_conf = (bets[i]['confidence'] + bets[j]['confidence'] + 
                                   bets[k]['confidence']) / 3
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

class ResultChecker:
    def __init__(self, api: FootballAPI, db: DatabaseManager):
        self.api = api
        self.db = db
    
    async def check_pending_bets(self) -> List[Tuple[int, str, float]]:
        """Проверява всички чакащи залози"""
        pending = self.db.get_pending_bets()
        results = []
        
        for bet in pending:
            try:
                all_finished = True
                all_won = True
                
                for fixture_info in bet['fixtures']:
                    fixture_id = fixture_info['fixture_id']
                    result = await self.api.get_fixture_result(fixture_id)
                    
                    if not result:
                        all_finished = False
                        break
                    
                    status = result['fixture']['status']['short']
                    if status not in ['FT', 'AET', 'PEN']:
                        all_finished = False
                        break
                    
                    # Check if bet won
                    won = self._check_bet_result(result, fixture_info)
                    if not won:
                        all_won = False
                
                if all_finished:
                    if all_won:
                        profit = bet['amount'] * bet['odd'] - bet['amount']
                        results.append((bet['id'], 'won', profit))
                    else:
                        results.append((bet['id'], 'lost', -bet['amount']))
                
                await asyncio.sleep(1)
                
            except Exception as e:
                logger.error(f"Check error: {e}")
        
        return results
    
    def _check_bet_result(self, result: Dict, bet_info: Dict) -> bool:
        """Проверява дали конкретен залог е спечелен"""
        try:
            prediction_key = bet_info.get('prediction_key', '')
            goals = result['goals']
            home_goals = goals['home']
            away_goals = goals['away']
            
            if prediction_key == 'home':
                return home_goals > away_goals
            elif prediction_key == 'away':
                return away_goals > home_goals
            elif prediction_key == 'draw':
                return home_goals == away_goals
            elif 'Over' in prediction_key:
                total = float(prediction_key.split()[1])
                return (home_goals + away_goals) > total
            elif 'Under' in prediction_key:
                total = float(prediction_key.split()[1])
                return (home_goals + away_goals) < total
            elif prediction_key == 'btts_yes':
                return home_goals > 0 and away_goals > 0
            
        except Exception as e:
            logger.error(f"Result check error: {e}")
        
        return False

# Telegram Notification System with buttons
class TelegramNotifier:
    def __init__(self, token: str, channel_id: str, db: DatabaseManager):
        self.bot = Bot(token=token)
        self.channel_id = channel_id
        self.db = db
    
    async def send_bet_notification(self, combination: Dict, bet_amount: float, 
                                    bet_number: int):
        message = f"🎯 <b>ЗАЛОГ #{bet_number}</b>\n\n"
        message += f"💰 Сума: {bet_amount:.2f} EUR\n"
        message += f"📊 Коефициент: {combination['total_odd']:.2f}\n"
        message += f"✅ Вероятност: {combination['avg_confidence']:.1f}%\n"
        message += f"💵 Печалба: {bet_amount * combination['total_odd']:.2f} EUR\n\n"
        
        message += "<b>══════════════</b>\n"
        
        for idx, bet in enumerate(combination['bets'], 1):
            fixture = bet['fixture']
            home = fixture['teams']['home']['name']
            away = fixture['teams']['away']['name']
            
            try:
                time = datetime.fromisoformat(
                    fixture['fixture']['date'].replace('Z', '+00:00')
                ).astimezone(BG_TZ)
                time_str = time.strftime('%H:%M')
            except:
                time_str = "TBA"
            
            message += f"<b>{idx}. {home} vs {away}</b>\n"
            message += f"   🕐 {time_str}\n"
            message += f"   🎲 {bet['type']}\n"
            message += f"   📈 @ {bet['odd']:.2f}\n\n"
        
        try:
            await self.bot.send_message(
                chat_id=self.channel_id,
                text=message,
                parse_mode='HTML'
            )
            logger.info(f"Sent bet #{bet_number}")
        except TelegramError as e:
            logger.error(f"Telegram error: {e}")
    
    async def send_result_notification(self, bet_id: int, result: str, profit: float):
        emoji = "🎉" if result == "won" else "😔"
        message = f"{emoji} <b>РЕЗУЛТАТ ЗАЛОГ #{bet_id}</b>\n\n"
        
        if result == "won":
            message += f"✅ СПЕЧЕЛЕН!\n💰 Печалба: +{profit:.2f} EUR"
        else:
            message += f"❌ ЗАГУБЕН\n💸 Загуба: {profit:.2f} EUR"
        
        try:
            await self.bot.send_message(
                chat_id=self.channel_id,
                text=message,
                parse_mode='HTML'
            )
        except:
            pass
    
    async def send_daily_summary(self, stats: Dict):
        message = f"📊 <b>ДНЕВЕН ОТЧЕТ</b>\n\n"
        message += f"🎲 Общо залози: {stats['total_bets']}\n"
        message += f"✅ Спечелени: {stats['won_bets']}\n"
        message += f"❌ Загубени: {stats['lost_bets']}\n"
        message += f"⏳ В ход: {stats['pending_bets']}\n\n"
        message += f"💰 Заложени: {stats['total_staked']:.2f} EUR\n"
        message += f"💵 Печалба/Загуба: {stats['total_profit']:.2f} EUR\n"
        message += f"📈 Success Rate: {stats['success_rate']:.1f}%"
        
        try:
            await self.bot.send_message(
                chat_id=self.channel_id,
                text=message,
                parse_mode='HTML'
            )
        except:
            pass

# Web endpoints
async def health_check(request):
    now = datetime.now(BG_TZ)
    return web.Response(text=f"Bot v2.0 Running!\nBG Time: {now.strftime('%H:%M:%S')}")

async def status(request):
    now = datetime.now(BG_TZ)
    return web.json_response({
        "status": "active",
        "version": "2.0",
        "bg_time": now.strftime('%Y-%m-%d %H:%M:%S')
    })

async def keep_alive():
    while True:
        try:
            await asyncio.sleep(600)
            async with aiohttp.ClientSession() as session:
                async with session.get('http://localhost:10000/') as resp:
                    logger.info(f"Keepalive: {resp.status}")
        except:
            pass

# Main bot loop
async def bot_loop():
    api = FootballAPI(API_FOOTBALL_KEY)
    db = DatabaseManager()
    selector = AdvancedBetSelector(api)
    strategy = BettingStrategy(db)
    notifier = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, db)
    result_checker = ResultChecker(api, db)
    
    logger.info("Advanced Bot v2.0 Starting!")
    
    used_fixture_ids = []
    last_check_date = None
    last_result_check = datetime.now(BG_TZ)
    last_daily_summary = None
    
    while True:
        try:
            now = datetime.now(BG_TZ)
            current_date = now.date()
            current_hour = now.hour
            
            # New day reset
            if last_check_date != current_date:
                strategy.reset_daily()
                used_fixture_ids = []
                last_check_date = current_date
                logger.info(f"NEW DAY: {current_date}")
            
            # Check results every 30 minutes
            if (now - last_result_check).seconds > 1800:
                logger.info("Checking pending bet results...")
                results = await result_checker.check_pending_bets()
                
                for bet_id, result, profit in results:
                    db.update_bet_result(bet_id, result, profit)
                    await notifier.send_result_notification(bet_id, result, profit)
                    
                    # Update martingale
                    strategy.calculate_next_bet(result == 'won')
                
                last_result_check = now
            
            # Daily summary at 23:00
            if current_hour == 23 and last_daily_summary != current_date:
                stats = db.get_daily_stats(str(current_date))
                await notifier.send_daily_summary(stats)
                last_daily_summary = current_date
            
            # Smart betting - every 2 hours or when we have < 3 bets
            should_search = False
            
            if current_hour in [8, 10, 12, 14, 16, 18, 20]:
                if len(strategy.bets_today) < MAX_BETS_PER_DAY:
                    should_search = True
            
            if should_search:
                logger.info(f"Smart search at {now.strftime('%H:%M')}")
                
                combination = await selector.find_smart_combination(used_fixture_ids)
                
                if combination:
                    bet_number = len(strategy.bets_today) + 1
                    bet_amount = strategy.current_bet
                    
                    # Save to DB
                    bet_data = {
                        'bet_number': bet_number,
                        'date': str(current_date),
                        'amount': bet_amount,
                        'odd': combination['total_odd'],
                        'potential_win': bet_amount * combination['total_odd'],
                        'bet_type': ', '.join([b['bet_category'] for b in combination['bets']]),
                        'fixtures': [{
                            'fixture_id': b['fixture_id'],
                            'home': b['fixture']['teams']['home']['name'],
                            'away': b['fixture']['teams']['away']['name'],
                            'prediction_key': b['prediction_key']
                        } for b in combination['bets']]
                    }
                    
                    db.save_bet(bet_data)
                    
                    await notifier.send_bet_notification(combination, bet_amount, bet_number)
                    
                    strategy.bets_today.append(combination)
                    
                    for bet in combination['bets']:
                        used_fixture_ids.append(bet['fixture_id'])
                    
                    logger.info(f"Bet #{bet_number} placed!")
                else:
                    logger.info("No suitable combination found")
            
            await asyncio.sleep(300)  # 5 minutes
            
        except Exception as e:
            logger.error(f"Main loop error: {e}")
            logger.error(traceback.format_exc())
            await asyncio.sleep(60)

# Telegram bot commands handler
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📊 Статистики", callback_data='stats')],
        [InlineKeyboardButton("🎲 Ръчен залог", callback_data='manual_bet')],
        [InlineKeyboardButton("⚙️ Настройки", callback_data='settings')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        'Добре дошли в Advanced Football Bot v2.0!\n\n'
        'Използвайте бутоните за управление:',
        reply_markup=reply_markup
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    db = DatabaseManager()
    
    if query.data == 'stats':
        today = datetime.now(BG_TZ).date()
        stats = db.get_daily_stats(str(today))
        
        message = f"📊 <b>СТАТИСТИКИ ЗА ДНЕС</b>\n\n"
        message += f"🎲 Залози: {stats['total_bets']}\n"
        message += f"✅ Спечелени: {stats['won_bets']}\n"
        message += f"❌ Загубени: {stats['lost_bets']}\n"
        message += f"⏳ Чакащи: {stats['pending_bets']}\n\n"
        message += f"💰 Заложени: {stats['total_staked']:.2f} EUR\n"
        message += f"💵 Печалба: {stats['total_profit']:.2f} EUR\n"
        message += f"📈 Success: {stats['success_rate']:.1f}%"
        
        await query.edit_message_text(message, parse_mode='HTML')
    
    elif query.data == 'manual_bet':
        await query.edit_message_text(
            "🎲 Ръчен залог стартира...\n"
            "Моля изчакайте 1-2 минути..."
        )
        
        # Trigger manual search
        api = FootballAPI(API_FOOTBALL_KEY)
        selector = AdvancedBetSelector(api)
        
        try:
            combination = await selector.find_smart_combination([])
            
            if combination:
                message = "✅ Намерена комбинация!\n\n"
                message += f"Коефициент: {combination['total_odd']:.2f}\n"
                message += f"Вероятност: {combination['avg_confidence']:.1f}%\n\n"
                
                for idx, bet in enumerate(combination['bets'], 1):
                    fixture = bet['fixture']
                    message += f"{idx}. {fixture['teams']['home']['name']} vs "
                    message += f"{fixture['teams']['away']['name']}\n"
                    message += f"   {bet['type']} @ {bet['odd']:.2f}\n\n"
                
                await query.edit_message_text(message)
            else:
                await query.edit_message_text(
                    "❌ Не са намерени подходящи комбинации в момента."
                )
        except Exception as e:
            await query.edit_message_text(f"Грешка: {str(e)}")
    
    elif query.data == 'settings':
        message = "⚙️ <b>НАСТРОЙКИ</b>\n\n"
        message += f"💰 Начална сума: {INITIAL_BET} EUR\n"
        message += f"📊 Целеви коефициент: {TARGET_ODD_MIN}-{TARGET_ODD_MAX}\n"
        message += f"🎲 Макс залози/ден: {MAX_BETS_PER_DAY}\n"
        message += f"📈 Мартингейл: x{MARTINGALE_MULTIPLIER}"
        
        await query.edit_message_text(message, parse_mode='HTML')

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = DatabaseManager()
    today = datetime.now(BG_TZ).date()
    stats = db.get_daily_stats(str(today))
    
    message = f"📊 <b>ДНЕВНИ СТАТИСТИКИ</b>\n\n"
    message += f"🎲 Общо залози: {stats['total_bets']}\n"
    message += f"✅ Спечелени: {stats['won_bets']}\n"
    message += f"❌ Загубени: {stats['lost_bets']}\n"
    message += f"⏳ В ход: {stats['pending_bets']}\n\n"
    message += f"💰 Заложени: {stats['total_staked']:.2f} EUR\n"
    message += f"💵 Печалба/Загуба: {stats['total_profit']:.2f} EUR\n"
    message += f"📈 Success Rate: {stats['success_rate']:.1f}%"
    
    await update.message.reply_text(message, parse_mode='HTML')

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
    # Web server
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/status', status)
    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)
    
    logger.info(f"Advanced Bot v2.0 on port {PORT}")
    web.run_app(app, host='0.0.0.0', port=PORT)
