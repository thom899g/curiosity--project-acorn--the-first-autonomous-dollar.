"""
Project Acorn - Phase 1: Core Cloud Functions
Architected for production resilience with comprehensive error handling
"""

import os
import logging
import uuid
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

# Firebase imports
import firebase_admin
from firebase_admin import firestore, credentials

# Trading imports
import ccxt
from google.cloud import secretmanager

# Telegram notifications
import requests

# Initialize logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Firebase (will be auto-initialized in Cloud Functions)
try:
    if not firebase_admin._apps:
        cred = credentials.ApplicationDefault()
        firebase_admin.initialize_app(cred)
    db = firestore.client()
except Exception as e:
    logger.error(f"Firebase initialization failed: {e}")
    raise

# Constants
PROJECT_ID = os.environ.get('GCP_PROJECT', 'project-acorn')
BINANCE_TESTNET = True  # Change to False for production
SYMBOL = 'BNB/USDT'
DAILY_AMOUNT = 1.0  # $1 daily
PROFIT_TARGET = 0.008  # 0.8% after fees
PROFIT_THRESHOLD = 10.0  # $10 minimum for withdrawal


class SecretManager:
    """Secure secret retrieval with caching"""
    
    _secrets_cache = {}
    
    @staticmethod
    def get_secret(secret_id: str) -> str:
        """Retrieve secret from GCP Secret Manager"""
        if secret_id in SecretManager._secrets_cache:
            return SecretManager._secrets_cache[secret_id]
        
        try:
            client = secretmanager.SecretManagerServiceClient()
            name = f"projects/{PROJECT_ID}/secrets/{secret_id}/versions/latest"
            response = client.access_secret_version(request={"name": name})
            secret_value = response.payload.data.decode("UTF-8")
            SecretManager._secrets_cache[secret_id] = secret_value
            return secret_value
        except Exception as e:
            logger.error(f"Failed to retrieve secret {secret_id}: {e}")
            raise


class BinanceClient:
    """Binance exchange client with error resilience"""
    
    def __init__(self):
        self.api_key = SecretManager.get_secret('binance-api-key')
        self.api_secret = SecretManager.get_secret('binance-api-secret')
        
        self.exchange = ccxt.binance({
            'apiKey': self.api_key,
            'secret': self.api_secret,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'spot',
                'adjustForTimeDifference': True,
            }
        })
        
        if BINANCE_TESTNET:
            self.exchange.urls['api'] = self.exchange.urls['test']
            logger.info("Using Binance testnet")
    
    def get_ticker(self, symbol: str = SYMBOL) -> Optional[Dict]:
        """Get current market price with error handling"""
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            return {
                'symbol': symbol,
                'price': float(ticker['last']),
                'timestamp': datetime.utcnow().isoformat(),
                'bid': float(ticker['bid']),
                'ask': float(ticker['ask']),
                'volume': float(ticker['quoteVolume'])
            }
        except ccxt.NetworkError as e:
            logger.error(f"Network error fetching ticker: {e}")
            return None
        except ccxt.ExchangeError as e:
            logger.error(f"Exchange error fetching ticker: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error fetching ticker: {e}")
            return None
    
    def place_market_buy(self, symbol: str, amount: float) -> Optional[Dict]:
        """Place market buy order with slippage protection"""
        try:
            # Get current price for validation
            ticker = self.get_ticker(symbol)
            if not ticker:
                return None
            
            # Calculate quantity with 2% slippage buffer
            price = ticker['price']
            max_slippage = price * 0.02
            max_price = price + max_slippage
            
            order = self.exchange.create_order(
                symbol=symbol,
                type='market',
                side='buy',
                amount=amount / price,  # Convert USD to BNB quantity
                params={'price': max_price}
            )
            
            return {
                'order_id': order['id'],
                'symbol': order['symbol'],
                'type': order['type'],
                'side': order['side'],
                'amount': float(order['amount']),
                'price': float(order['price'] or price),
                'status': order['status'],
                'timestamp': datetime.utcnow().isoformat(),
                'fee': float(order['fee']['cost']) if order.get('fee') else 0.0
            }
        except ccxt.InsufficientFunds as e:
            logger.error(f"Insufficient funds: {e}")
            return None
        except ccxt.InvalidOrder as e:
            logger.error(f"Invalid order: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to place buy order: {e}")
            return None
    
    def place_limit_sell(self, symbol: str, quantity: float, buy_price: float) -> Optional[Dict]:
        """Place limit sell order at profit target"""
        try:
            # Calculate sell price with profit target
            sell_price = round(buy_price * (1 + PROFIT_TARGET), 6)
            
            order = self.exchange.create_order(
                symbol=symbol,
                type='limit',
                side='sell',
                amount=quantity,
                price=sell_price
            )
            
            return {
                'order_id': order['id'],
                'symbol': order['symbol'],
                'type': order['type'],
                'side': order['side'],
                'amount': float(order['amount']),
                'price': float(order['price']),
                'status': order['status'],
                'timestamp': datetime.utcnow().isoformat(),
                'target_profit_percent': PROFIT_TARGET * 100
            }
        except Exception as e:
            logger.error(f"Failed to place sell order: {e}")
            return None
    
    def withdraw_to_vault(self, amount: float, currency: str = 'USDT') -> Optional[Dict]:
        """Withdraw profit to hardware vault"""
        try:
            address = SecretManager.get_secret('hardware-vault-address')
            
            withdrawal = self.exchange.withdraw(
                currency,
                amount,
                address,
                params={'network': 'BSC'}  # Using BSC for lower fees
            )
            
            return {
                'withdrawal_id': withdrawal['id'],
                'amount': amount,
                'currency': currency,
                'address': address,
                'tx_hash': withdrawal.get('txid'),
                'timestamp': datetime.utcnow().isoformat(),
                'status': 'pending'
            }
        except Exception as e:
            logger.error(f"Withdrawal failed: {e}")
            return None


class TelegramNotifier:
    """Send notifications via Telegram"""
    
    def __init__(self):
        self.bot_token = SecretManager.get_secret('telegram-bot-token')
        self.chat_id = SecretManager.get_secret('telegram-chat-id')
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
    
    def send_message(self, text: str, parse_mode: str = 'HTML') -> bool:
        """Send message to Telegram channel"""
        try:
            url = f"{self.base_url}/sendMessage"
            payload = {
                'chat_id': self.chat_id,
                'text': text,
                'parse_mode': parse_mode
            }
            response = requests.post(url,