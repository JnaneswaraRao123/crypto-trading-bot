# Simplified Crypto Trading Bot (Binance Futures Testnet)

## Overview
This project is a simplified trading bot built using Python that interacts with the Binance Futures Testnet (USDT-M).  
It supports placing MARKET, LIMIT, and TWAP orders via the official Binance REST API.

## Features
- Binance Futures Testnet integration
- Market and Limit orders
- BUY and SELL support
- Optional TWAP order type
- Command-line interface (CLI)
- Input validation and structured logging
- Error handling for API and network issues

## Technologies
- Python 3.12
- Binance Futures REST API
- requests library
- argparse & logging

## How to Run
```bash
python task1.py \
  --api-key <API_KEY> \
  --api-secret <API_SECRET> \
  --symbol BTCUSDT \
  --side BUY \
  --order-type MARKET \
  --quantity 0.001
