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
API_CALL_INTERVAL = 6 * 60  # 6 minutes in seconds

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

if __name__ == "__main__":
    can_call, wait_time = can_make_api_call()
    if not can_call:
        print(f"API calls can only be made every 6 minutes. Please wait {int(wait_time // 60)} minutes and {int(wait_time % 60)} seconds before trying again.")
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
                    # Use PetSafe's official endpoint + your feeder's thing_name
                    thing_name = data["thing_name"]
                    history_url = f"https://platform.cloud.petsafe.net/smart-feed/feeders/{thing_name}/messages?days=7"
                    
                    # Set Authorization to your ID token (no "Bearer" prefix)
                    headers = {
                        "Content-Type": "application/json",
                        "Authorization": tokens["id_token"],
                    }
                    
                    response = requests.get(history_url, headers=headers)
                    response.raise_for_status()
                    
                    feed_messages = response.json()  # Should be a list of message objects
                    logger.debug(f"Raw feed messages: {json.dumps(feed_messages, indent=2)}")
                    
                    # Filter for all FEED_DONE messages
                    feed_done_messages = [
                        msg for msg in feed_messages if msg["message_type"] == "FEED_DONE" and 'timestamp' in msg
                    ]
                    
                    if feed_done_messages:
                        print("        Feeding history (last 7 days):")
                        for msg in feed_done_messages:
                            # Format date and time
                            date_time = time.strptime(msg['timestamp'], '%Y-%m-%dT%H:%M:%S.%fZ')
                            formatted_date = time.strftime('%a %d %b %y', date_time)
                            formatted_time = time.strftime('%H:%M', date_time)
                            
                            # Remove unwanted fields from payload
                            payload = msg.get('payload', {})
                            payload.pop('isfoodlow', None)
                            payload.pop('schedule', None)
                            payload.pop('time', None)
                            
                            print(f"        Date: {formatted_date}")
                            print(f"        Time: {formatted_time}")
                            print(f"        Payload: {payload}")
                    else:
                        print("        No FEED_DONE events found in the last 7 days!")

                except Exception as e:
                    print(f"        Error fetching history: {str(e)}")
                    logger.debug(f"Error fetching history: {str(e)}")

    except Exception as e:
        print(f"Error: {str(e)}")
        logger.debug(f"Root-level exception details: {str(e)}")
    finally:
        log_api_call()  # Log the API call time