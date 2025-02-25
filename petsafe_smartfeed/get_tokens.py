import imaplib
import email
import email.utils
import time
from petsafe_smartfeed.client import PetSafeClient
import re
import logging
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
import yaml
import json
import traceback

# Configure logging
logging.basicConfig(
    level=logging.INFO,  # Changed from DEBUG to INFO
    format='%(message)s',  # Simplified format to show only the message
    handlers=[
        logging.StreamHandler(),  # Console output
        logging.FileHandler('petsafe_auth.log')  # Full logs still saved to file
    ]
)
logger = logging.getLogger(__name__)

# Suppress noisy libraries
logging.getLogger('botocore').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('boto3').setLevel(logging.WARNING)

def get_latest_petsafe_code(email_address, app_password, wait_time=20):
    try:
        logger.info(f"Attempting to connect to Gmail IMAP for {email_address}")
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(email_address, app_password)
        
        status, _ = mail.select("INBOX")
        if status != 'OK':
            raise Exception("Could not access inbox")

        if wait_time:
            logger.info(f"Waiting {wait_time} seconds for email to arrive")
            time.sleep(wait_time)

        # Search for recent PetSafe emails (format: DD-MMM-YYYY)
        search_date = (datetime.now() - timedelta(minutes=10)).strftime("%d-%b-%Y")
        # IMAP search criteria needs to be separate arguments
        status, messages = mail.search(None, 
                                     'FROM', 'no-reply@directory.cloud.petsafe.net',
                                     'SINCE', search_date)
        
        if status != 'OK' or not messages[0]:
            raise Exception("No recent PetSafe verification emails found")

        # Get the latest email
        email_ids = messages[0].split()
        if not email_ids:
            raise Exception("No matching emails found")
            
        latest_email_id = email_ids[-1]
        
        # Fetch email content
        _, msg_data = mail.fetch(latest_email_id, "(RFC822)")
        if not msg_data or not msg_data[0]:
            raise Exception("Could not fetch email content")
            
        email_message = email.message_from_bytes(msg_data[0][1])
        
        code = None
        if email_message.is_multipart():
            for part in email_message.walk():
                if part.get_content_type() == "text/plain":
                    body = part.get_payload(decode=True).decode()
                    patterns = [
                        r'verification code is: (\d{6})',
                        r'Your 6-Digit PIN is:\s*(\d{6})',
                        r'code:\s*(\d{6})'
                    ]
                    for pattern in patterns:
                        match = re.search(pattern, body)
                        if match:
                            code = match.group(1)
                            break
                    if code:
                        break

        mail.close()
        mail.logout()
        
        if not code:
            raise Exception("Verification code not found in email")

        return code

    except Exception as e:
        logger.error(f"Error in get_latest_petsafe_code: {str(e)}")
        logger.error(traceback.format_exc())
        raise

def authenticate_petsafe(login_email, retrieve_email, app_password, debug_only=False):
    try:
        print("Starting PetSafe authentication process...")
        client = PetSafeClient(email=login_email)
        
        if not debug_only:
            print("Requesting verification code...")
            client.request_code()
            wait_time = 30
        else:
            print("Debug mode: Skipping code request")
            wait_time = 0
        
        print(f"Checking email for verification code...")
        code = get_latest_petsafe_code(retrieve_email, app_password, wait_time=wait_time)
        print(f"Verification code found: {code}")
        
        if debug_only:
            return {"debug_code": code}
        
        # Add a small delay before using the code
        print("Waiting for code activation...")
        time.sleep(5)  # Wait 5 seconds before using the code
        
        print("Requesting authentication tokens...")
        response = client.request_tokens_from_code(code)
        
        if 'AuthenticationResult' not in response:
            # If first attempt fails, wait a bit longer and try once more
            print("First attempt failed, retrying...")
            time.sleep(5)  # Wait additional 5 seconds
            response = client.request_tokens_from_code(code)
            if 'AuthenticationResult' not in response:
                raise Exception(f"Authentication failed: Invalid response")
        
        token = response['AuthenticationResult']
        print("Authentication successful!")
        return {
            "id_token": token['IdToken'],
            "refresh_token": token['RefreshToken'],
            "access_token": token['AccessToken']
        }
    
    except Exception as e:
        print(f"Error: {str(e)}")
        logger.error(traceback.format_exc())
        raise

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--debug', action='store_true', help='Check existing emails only')
    args = parser.parse_args()

    try:
        print("Loading configuration...")
        config_path = os.path.join(os.path.dirname(__file__), 'config.yaml')
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)

        tokens = authenticate_petsafe(
            config['login_email'],
            config['retrieve_email'],
            config['app_password'], 
            debug_only=args.debug
        )
        
        if args.debug:
            print(f"Debug code: {tokens['debug_code']}")
        else:
            token_file = os.path.join(os.path.dirname(__file__), 'tokens.json')
            if os.path.exists(token_file):
                os.remove(token_file)
                print("Previous tokens.json file deleted")
            
            token_data = {
                "id_token": tokens['id_token'],
                "refresh_token": tokens['refresh_token'],
                "access_token": tokens['access_token'],
                "email": config['login_email'],
                "token_expires": time.time() + 3600
            }
            
            with open(token_file, 'w') as f:
                json.dump(token_data, f, indent=2)
            print("New tokens.json file created successfully")
            
    except Exception as e:
        print(f"Operation failed: {str(e)}")
        sys.exit(1)