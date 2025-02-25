import petsafe_smartfeed as sf
import json
import requests
import os
import sys
import time
import logging
import boto3
import botocore.config
import botocore.loaders
import argparse
from datetime import datetime, timedelta
import subprocess

# Setup logging with filters
class SensitiveDataFilter(logging.Filter):
    def filter(self, record):
        if 'Making request' in str(record.msg):
            return False
        if 'InitiateAuth' in str(record.msg):
            return False
        return True

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
logger.addFilter(SensitiveDataFilter())

# Also filter botocore logger
boto_logger = logging.getLogger('botocore')
boto_logger.addFilter(SensitiveDataFilter())
boto_logger.setLevel(logging.WARNING)

# Configure boto3 to skip metadata service lookup
os.environ['AWS_EC2_METADATA_DISABLED'] = 'true'
boto3.Session(aws_access_key_id='none', aws_secret_access_key='none')
config = botocore.config.Config(
    connect_timeout=5,
    retries={'max_attempts': 0}
)

LAST_API_CALL_LOG = 'last_api_call.log'
API_CALL_INTERVAL = 1 * 60  # 1 minute in seconds

def can_make_api_call():
    if os.path.exists(LAST_API_CALL_LOG):
        with open(LAST_API_CALL_LOG, 'r') as f:
            last_call_time = float(f.read().strip())
            elapsed_time = time.time() - last_call_time
            if elapsed_time < API_CALL_INTERVAL:
                return False, API_CALL_INTERVAL - elapsed_time
    return True, 0

def log_api_call():
    with open(LAST_API_CALL_LOG, 'w') as f:
        f.write(str(time.time()))

def save_raw_results(feed_messages, filename):
    """Save raw API response to a JSON file"""
    try:
        with open(filename, 'w') as f:
            json.dump(feed_messages, f, indent=2)
        logger.info(f"Raw results saved to {filename}")
    except Exception as e:
        logger.error(f"Failed to save raw results: {str(e)}")

def load_raw_results(filename):
    """Load raw results from a JSON file"""
    try:
        with open(filename, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"Error: {filename} not found. Please run the script without --dry-run first.")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: {filename} is malformed: {str(e)}")
        sys.exit(1)

def load_tokens():
    try:
        with open('tokens.json', 'r') as f:
            tokens = json.load(f)
            
        # Check if tokens have expired
        if time.time() > tokens['token_expires']:
            print("\nTokens have expired. Starting automatic refresh...")
            print("Running get_tokens.py...\n")
            result = subprocess.run(
                [sys.executable, 'get_tokens.py'],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT
            )
            # Print the output from get_tokens.py
            print(result.stdout)
            print("Token refresh completed. Reloading tokens...\n")
            
            # Reload the new tokens
            with open('tokens.json', 'r') as f:
                tokens = json.load(f)
            
        # Ensure we're using the correct email
        if tokens['email'] != "massavero@gmail.com":
            print(f"Error: tokens.json contains wrong email ({tokens['email']}). "
                  f"Please run get_tokens.py to get tokens for massavero@gmail.com")
            sys.exit(1)
            
        return tokens
    except FileNotFoundError:
        print("\ntokens.json not found.")
        print("Running get_tokens.py to create new tokens...\n")
        result = subprocess.run(
            [sys.executable, 'get_tokens.py'],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT
        )
        # Print the output from get_tokens.py
        print(result.stdout)
        return load_tokens()
    except json.JSONDecodeError as e:
        print(f"Error: tokens.json is malformed: {str(e)}")
        sys.exit(1)

def filter_feed_messages(feed_messages):
    now = datetime.utcnow()  # Use offset-naive datetime
    seven_days_ago = now - timedelta(days=7)
    filtered_messages = []
    for msg in feed_messages:
        if 'created_at' not in msg:
            logger.warning(f"Message missing created_at: {msg}")
            continue
        try:
            msg_time = datetime.strptime(msg['created_at'], '%Y-%m-%d %H:%M:%S')
            if msg_time > seven_days_ago:
                filtered_messages.append(msg)
        except ValueError as e:
            logger.warning(f"Error parsing created_at for message: {msg}, error: {str(e)}")
    return filtered_messages

def process_feed_messages(feed_messages):
    """Helper function to process messages consistently"""
    filtered_messages = []
    seven_days_ago = datetime.now() - timedelta(days=7)
    
    for msg in feed_messages:
        if msg.get("message_type") != "FEED_DONE":
            continue

        # Convert date and check if it's within last 7 days
        try:
            dt = datetime.strptime(msg['created_at'], '%Y-%m-%d %H:%M:%S')
            if dt < seven_days_ago:
                continue
            formatted_date = dt.strftime('%a %d %b %Y %H:%M')
        except (ValueError, KeyError):
            continue

        # Create clean message (removed status field)
        clean_msg = {
            'created_at': formatted_date,
            'amount': msg.get('payload', {}).get('amount', 'unknown'),
        }
        
        # Get source from payload
        source = msg.get('payload', {}).get('source', '')
        clean_msg['feed_type'] = 'SCHEDULED FEED' if source == 'schedule' else 'MANUAL FEED'
        
        filtered_messages.append(clean_msg)

    # Sort by date, newest first
    filtered_messages.sort(key=lambda x: datetime.strptime(x['created_at'], '%a %d %b %Y %H:%M'), reverse=True)
    return filtered_messages

def calculate_food_remaining(feed_messages, full_sensor1=30103, full_sensor2=30101, empty_sensor1=5273, empty_sensor2=2231, total_food_grams=2770, portion_grams=10):
    """Calculate remaining food and consumption patterns
    
    Args:
        feed_messages: List of feeding events
        full_sensor1/2: Calibrated full readings for both sensors
        empty_sensor1/2: Calibrated empty readings for both sensors
        total_food_grams: Total capacity in grams
        portion_grams: Grams per portion
    """
    # Filter only FEED_DONE messages and sort by date
    feed_done_messages = [msg for msg in feed_messages if msg['message_type'] == 'FEED_DONE']
    feed_done_messages.sort(key=lambda x: x['created_at'], reverse=True)
    
    if not feed_done_messages:
        return {
            'percent_remaining': 0,
            'remaining_grams': 0,
            'days_of_food_left': 0,
            'daily_consumption': 0,
            'scheduled_daily': 0,
            'manual_daily': 0
        }

    # Get the most recent sensor readings
    current_sensor1 = feed_done_messages[0]['payload'].get('sensorReading1Infrared', full_sensor1)
    current_sensor2 = feed_done_messages[0]['payload'].get('sensorReading2Infrared', full_sensor2)
    
    # Calculate percentage for each sensor with bounds checking
    def calculate_sensor_percentage(current, full, empty):
        if current <= empty:
            return 0
        if current >= full:
            return 100
        return ((current - empty) / (full - empty)) * 100

    sensor1_percent = calculate_sensor_percentage(current_sensor1, full_sensor1, empty_sensor1)
    sensor2_percent = calculate_sensor_percentage(current_sensor2, full_sensor2, empty_sensor2)
    
    # Weight the sensors (could be adjusted based on reliability)
    avg_percent = (sensor1_percent * 0.5) + (sensor2_percent * 0.5)
    
    # Calculate remaining food in grams
    remaining_food_grams = total_food_grams * (avg_percent / 100)

    # Analyze feeding patterns over the last 7 days
    now = datetime.strptime(feed_done_messages[0]['created_at'], '%Y-%m-%d %H:%M:%S')
    week_ago = now - timedelta(days=7)
    
    week_feeds = [
        msg for msg in feed_done_messages 
        if datetime.strptime(msg['created_at'], '%Y-%m-%d %H:%M:%S') > week_ago
    ]

    # Separate scheduled and manual feeds
    scheduled_feeds = [
        feed for feed in week_feeds 
        if feed.get('payload', {}).get('source') == 'schedule'
    ]
    manual_feeds = [
        feed for feed in week_feeds 
        if feed.get('payload', {}).get('source') != 'schedule'
    ]

    # Calculate daily averages
    days_analyzed = min(7, (now - datetime.strptime(week_feeds[-1]['created_at'], '%Y-%m-%d %H:%M:%S')).days + 1)
    
    total_scheduled = sum(feed.get('payload', {}).get('amount', 0) for feed in scheduled_feeds)
    total_manual = sum(feed.get('payload', {}).get('amount', 0) for feed in manual_feeds)
    
    scheduled_daily = (total_scheduled * portion_grams) / days_analyzed
    manual_daily = (total_manual * portion_grams) / days_analyzed
    daily_consumption = scheduled_daily + manual_daily

    # Calculate days of food left with safety margin
    days_of_food_left = (remaining_food_grams / daily_consumption) * 0.9 if daily_consumption > 0 else 0

    return {
        'percent_remaining': round(avg_percent, 2),
        'remaining_grams': round(remaining_food_grams, 2),
        'days_of_food_left': round(days_of_food_left, 2),
        'daily_consumption': round(daily_consumption, 2),
        'scheduled_daily': round(scheduled_daily, 2),
        'manual_daily': round(manual_daily, 2)
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true', help='Check existing raw results file instead of making API call')
    parser.add_argument('--last-feed-only', action='store_true', help='Print only the last feeding event without formatting')
    args = parser.parse_args()

    can_call, wait_time = can_make_api_call()
    if not can_call and not args.dry_run:
        print(f"API calls can only be made every 1 minute. Please wait {int(wait_time // 60)} minutes and {int(wait_time % 60)} seconds before trying again.")
        sys.exit(1)

    tokens = load_tokens()
    logger.debug(f"Loaded tokens for email: {tokens['email']}")
    logger.debug(f"Token expiration: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(tokens['token_expires']))}")

    # Add more detailed token debugging
    import jwt
    try:
        decoded_id = jwt.decode(tokens['id_token'], options={"verify_signature": False})
        decoded_access = jwt.decode(tokens['access_token'], options={"verify_signature": False})
        logger.debug("ID Token claims:")
        logger.debug(json.dumps(decoded_id, indent=2))
        logger.debug("Access Token claims:")
        logger.debug(json.dumps(decoded_access, indent=2))
        
        if decoded_id.get('email') != tokens['email']:
            logger.error(f"Token email mismatch: {decoded_id.get('email')} != {tokens['email']}")
            sys.exit(1)
    except Exception as e:
        logger.error(f"Failed to decode tokens: {str(e)}")

    logger.debug("Access token: " + tokens['access_token'][:20] + "...")
    logger.debug("ID token: " + tokens['id_token'][:20] + "...")

    client = sf.PetSafeClient(
        email=tokens['email'],
        id_token=tokens['id_token'],
        refresh_token=tokens['refresh_token'],
        access_token=tokens['access_token']
    )
    logger.debug("Client initialized")

    logger.debug("Testing client connection...")
    try:
        if args.dry_run:
            logger.info("Running in dry-run mode, loading raw results from file")
            feed_messages = load_raw_results('raw_feed_messages.json')
            # Print the raw JSON data with proper formatting
            print(json.dumps(feed_messages, indent=2))
            sys.exit(0)  # Exit cleanly after showing raw data

        print("\nAttempting to fetch feeders...")
        feeders = client.feeders
        logger.debug(f"Raw feeders response: {feeders}")
        
        if not feeders:
            print("No feeders found!")
            print("\nChecking API connection:")
            print(f"Using email: {tokens['email']}")
            print(f"Auth tokens present: {bool(client.access_token)}")
            print(f"Client authenticated: {bool(client.id_token)}")
            
            headers = {
                'Authorization': f'Bearer {client.access_token}',
                'Content-Type': 'application/json'
            }
            api_url = 'https://platform.cloud.petsafe.net/smart-feed/feeders'
            
            try:
                logger.debug("Making API request to validate connection...")
                logger.debug(f"Requesting URL: {api_url}")
                response = requests.get(api_url, headers=headers)
                logger.debug(f"Response headers: {dict(response.headers)}")

                print(f"\nAPI Response Status: {response.status_code}")
                print(f"API Response Content: {response.text}")
                
                if response.status_code == 403:
                    try:
                        decoded = jwt.decode(client.access_token, options={"verify_signature": False})
                        logger.debug("Token claims:")
                        logger.debug(json.dumps(decoded, indent=2))
                        if 'exp' in decoded:
                            exp_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(decoded['exp']))
                            logger.debug(f"Token expires at: {exp_time}")
                    except Exception as e:
                        logger.debug(f"Failed to decode token after 403 response: {str(e)}")
                        
            except requests.exceptions.RequestException as e:
                print(f"Error making feeder request: {str(e)}")
                logger.debug(f"Feeder request exception: {str(e)}")
        else:
            print(f"Found {len(feeders)} feeder(s)")
            for i, feeder in enumerate(feeders, 1):
                data = feeder.data
                settings = data.get('settings', {})
                schedules = data.get('schedules', [])
                
                print(f"\nFeeder {i}:")
                print(f"    Name: {settings.get('friendly_name', 'unnamed')}")
                print(f"    ID: {data.get('id')}")
                print(f"    Serial: {data.get('thing_name')}")
                print(f"    Model: {data.get('product_name')}")
                print(f"    Firmware: {data.get('firmware_version')}")
                print("\n    Status:")
                print(f"        Connected: {'Yes' if data.get('connection_status') == 2 else 'No'}")
                print(f"        Battery: {float(data.get('battery_voltage', 0))/1000:.2f}V")
                print(f"        Power Adapter: {'Yes' if data.get('is_adapter_installed') else 'No'}")
                print(f"        Food Low: {'Yes' if data.get('is_food_low') else 'No'}")
                
                print("\n    Settings:")
                print(f"        Pet Type: {settings.get('pet_type', 'unknown')}")
                print(f"        Timezone: {settings.get('timezone', 'unknown')}")
                print(f"        Slow Feed: {'Yes' if settings.get('slow_feed') else 'No'}")
                print(f"        Child Lock: {'Yes' if settings.get('child_lock') else 'No'}")
                print(f"        Paused: {'Yes' if settings.get('paused') else 'No'}")
                
                try:
                    thing_name = data["thing_name"]
                    history_url = f"https://platform.cloud.petsafe.net/smart-feed/feeders/{thing_name}/messages?days=7"
                    headers = {
                        "Content-Type": "application/json",
                        "Authorization": tokens["id_token"],
                    }
                    response = requests.get(history_url, headers=headers)
                    response.raise_for_status()
                    feed_messages = response.json()

                    save_raw_results(feed_messages, 'raw_feed_messages.json')

                    filtered_messages = process_feed_messages(feed_messages)
                    
                    food_status = calculate_food_remaining(feed_messages)
                    print("\n    Food Remaining:")
                    print(f"        Percentage: {int(food_status['percent_remaining'])}%")
                    print(f"        Weight: {food_status['remaining_grams'] / 10:.1f} hg")
                    print(f"        Daily Consumption: {food_status['daily_consumption']:.1f}g")
                    print(f"            Scheduled: {food_status['scheduled_daily']:.1f}g")
                    print(f"            Manual: {food_status['manual_daily']:.1f}g")
                    days = food_status['days_of_food_left']
                    print(f"        Days Left: {days:.1f} days ({days / 7:.1f} weeks)")

                    print("\n    Recent Feeding Events:")  # Added title
                    print("    " + "─" * 40)  # Added separator line

                    feed_done_events = [msg for msg in feed_messages if msg['message_type'] == 'FEED_DONE']
                    for event in feed_done_events:
                        created_at = event.get('created_at', 'Unknown Date')
                        amount = event.get('payload', {}).get('amount', 'Unknown')
                        source = event.get('payload', {}).get('source', 'Unknown')
                        print(f"        {created_at} - {amount} portions ({source})")

                except Exception as e:
                    print(f"        Error fetching history: {str(e)}")
                    logger.debug(f"Error fetching history: {str(e)}")

    except Exception as e:
        print(f"Error: {str(e)}")
        logger.debug(f"Root-level exception details: {str(e)}")
    finally:
        if not args.dry_run:
            log_api_call()  # Log the API call time