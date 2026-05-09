# ForrixGuard Monitor Dashboard

A lightweight web dashboard for monitoring ForrixGuard (Home Energy Management System) telemetry data using Redis streams and aiohttp with WebSockets.

## Features

- Real-time monitoring of meters and inverters
- Web-based dashboard with gauges and charts
- Redis streams integration for telemetry data
- WebSocket-based real-time updates
- Datasheet and topology visualization
- Multi-device support

## Requirements

- Python 3.11+
- Redis server
- aiohttp
- redis (asyncio support)

## Installation

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Ensure Redis is running on the specified host/port (default: localhost:6379)

## Usage

1. Run the dashboard:
   ```bash
   python3 dashboard.py
   ```

2. Open browser to: http://localhost:5000/inverter_dashboard.html

3. Connect to device streams by entering device ID and clicking Connect

## Configuration

- Set `REDIS_HOST` environment variable to your Redis server IP
- Update `TRAIT_TO_TEL_ID` mapping for your specific device_trait_id values

## Architecture

- Backend: aiohttp server with async Redis consumer
- Frontend: Static HTML/JS with WebSocket client
- Data: Redis streams (meters_stream, inverters_stream)
- Real-time: WebSocket push updates