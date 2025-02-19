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

def load_tokens():
    try:
        with open('tokens.json', 'r') as f:
            tokens = json.load(f)
            
        # Check if tokens have expired
        if time.time() > tokens['token_expires']:
            print("Error: Tokens have expired. Please run get_tokens.py to get new tokens.")
            sys.exit(1)
            
        # Ensure we're using the correct email
        if tokens['email'] != "massavero@gmail.com":
            print(f"Error: tokens.json contains wrong email ({tokens['email']}). "
                  f"Please run get_tokens.py to get tokens for massavero@gmail.com")
            sys.exit(1)
            
        return tokens
    except FileNotFoundError:
        print("Error: tokens.json not found. Please run get_tokens.py first.")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: tokens.json is malformed: {str(e)}")
        sys.exit(1)

def save_raw_results(feed_messages, filename):
    with open(filename, 'w') as f:
        json.dump(feed_messages, f, indent=2)
    logger.info(f"Raw results saved to {filename}")

def load_raw_results(filename):
    try:
        with open(filename, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"Error: {filename} not found. Please run the script without --dry-run first.")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: {filename} is malformed: {str(e)}")
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

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true', help='Check existing raw results file instead of making API call')
    parser.add_argument('--save', action='store_true', help='Save raw results to a JSON file')
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
            filtered_messages = process_feed_messages(feed_messages)
            
            if filtered_messages:
                print("\n        Recent feeding events:")
                for msg in filtered_messages:
                    feed_type = msg.pop('feed_type')
                    print(f"\n        {feed_type}")
                    for key, value in msg.items():
                        print(f"        {key}: {value}")
            else:
                print("        No feeding events in the last 7 days")
        else:
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
                    
                    print("\n    Feeding History:")
                    try:
                        # Get messages from API (existing code)
                        thing_name = data["thing_name"]
                        history_url = f"https://platform.cloud.petsafe.net/smart-feed/feeders/{thing_name}/messages?days=7"
                        headers = {
                            "Content-Type": "application/json",
                            "Authorization": tokens["id_token"],
                        }
                        response = requests.get(history_url, headers=headers)
                        response.raise_for_status()
                        feed_messages = response.json()

                        if args.save:
                            save_raw_results(feed_messages, 'raw_feed_messages.json')

                        filtered_messages = process_feed_messages(feed_messages)
                        
                        if filtered_messages:
                            print("\n        Recent feeding events:")
                            for msg in filtered_messages:
                                feed_type = msg.pop('feed_type')
                                print(f"\n        {feed_type}")
                                for key, value in msg.items():
                                    print(f"        {key}: {value}")
                        else:
                            print("        No feeding events in the last 7 days")

                    except Exception as e:
                        print(f"        Error fetching history: {str(e)}")
                        logger.debug(f"Error fetching history: {str(e)}")

    except Exception as e:
        print(f"Error: {str(e)}")
        logger.debug(f"Root-level exception details: {str(e)}")
    finally:
        if not args.dry_run:
            log_api_call()  # Log the API call time