from flask import Flask, jsonify, request
from petsafe_smartfeed.client import PetSafeClient
import json
import os
import yaml
import logging
from datetime import datetime, timedelta
import time
import subprocess
import sys

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('petsafe_bridge.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

def load_config():
    config_path = os.path.join(os.path.dirname(__file__), '..', 'config.yaml.python')
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

def load_tokens():
    try:
        token_path = os.path.join(os.path.dirname(__file__), '..', 'tokens.json')
        with open(token_path, 'r') as f:
            tokens = json.load(f)
            
        # Check if tokens have expired
        if time.time() > tokens['token_expires']:
            logger.info("Tokens expired, refreshing...")
            subprocess.run(
                [sys.executable, '../get_tokens.py'],
                check=True
            )
            # Reload the new tokens
            with open(token_path, 'r') as f:
                tokens = json.load(f)
                
        return tokens
    except Exception as e:
        logger.error(f"Error loading tokens: {e}")
        raise

def get_client():
    tokens = load_tokens()
    return PetSafeClient(
        email=tokens['email'],
        id_token=tokens['id_token'],
        refresh_token=tokens['refresh_token'],
        access_token=tokens['access_token']
    )

@app.route('/health')
def health_check():
    return jsonify({'status': 'healthy'})

@app.route('/status/<feeder_id>')
def get_status(feeder_id):
    try:
        client = get_client()
        feeders = client.feeders
        
        for feeder in feeders:
            if feeder.data['thing_name'] == feeder_id:
                config = load_config()
                
                # Calculate food level
                food_status = calculate_food_remaining(feeder)
                
                return jsonify({
                    'battery': float(feeder.data.get('battery_voltage', 0))/1000,
                    'connected': feeder.data.get('connection_status') == 2,
                    'food_low': feeder.data.get('is_food_low', False),
                    'adapter_installed': feeder.data.get('is_adapter_installed', False),
                    'food_level': food_status,
                    'last_feed': get_last_feed(feeder)
                })
        
        return jsonify({'error': 'Feeder not found'}), 404
        
    except Exception as e:
        logger.error(f"Error in get_status: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/feed/<feeder_id>', methods=['POST'])
def trigger_feed(feeder_id):
    try:
        portions = request.args.get('portions', default=1, type=int)
        client = get_client()
        feeders = client.feeders
        
        for feeder in feeders:
            if feeder.data['thing_name'] == feeder_id:
                # Trigger feed with specified portions
                feeder.feed(portions=portions)
                return jsonify({'status': 'success'})
                
        return jsonify({'error': 'Feeder not found'}), 404
        
    except Exception as e:
        logger.error(f"Error in trigger_feed: {e}")
        return jsonify({'error': str(e)}), 500

def calculate_food_remaining(feeder):
    """Calculate remaining food percentage"""
    try:
        config = load_config()
        full_sensor1 = 30103  # Calibrated values
        full_sensor2 = 30101
        empty_sensor1 = 5273
        empty_sensor2 = 2231
        
        current_sensor1 = feeder.data.get('sensorReading1Infrared', full_sensor1)
        current_sensor2 = feeder.data.get('sensorReading2Infrared', full_sensor2)
        
        # Calculate percentage for each sensor
        def calc_sensor_percentage(current, full, empty):
            if current <= empty:
                return 0
            if current >= full:
                return 100
            return ((current - empty) / (full - empty)) * 100
            
        sensor1_percent = calc_sensor_percentage(current_sensor1, full_sensor1, empty_sensor1)
        sensor2_percent = calc_sensor_percentage(current_sensor2, full_sensor2, empty_sensor2)
        
        # Average both sensors
        return (sensor1_percent + sensor2_percent) / 2
        
    except Exception as e:
        logger.error(f"Error calculating food level: {e}")
        return 0

def get_last_feed(feeder):
    """Get last feeding time and details"""
    try:
        history_url = f"https://platform.cloud.petsafe.net/smart-feed/feeders/{feeder.data['thing_name']}/messages?days=1"
        feed_messages = feeder.client.get(history_url)
        
        if feed_messages:
            feed_events = [msg for msg in feed_messages if msg['message_type'] == 'FEED_DONE']
            if feed_events:
                latest = feed_events[0]
                return {
                    'time': latest['created_at'],
                    'portions': latest.get('payload', {}).get('amount', 0),
                    'source': latest.get('payload', {}).get('source', 'unknown')
                }
    except Exception as e:
        logger.error(f"Error getting last feed: {e}")
    return None

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)