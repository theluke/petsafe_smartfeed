import imaplib
import email
import email.utils
import time
from petsafe_smartfeed.client import PetSafeClient
import re
import logging
import argparse
import os
from datetime import datetime, timedelta, timezone
import yaml
import json
import traceback

# Configure more detailed logging
logging.basicConfig(
    level=logging.DEBUG,  # Changed to DEBUG for more detailed logs
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),  # Console output
        logging.FileHandler('petsafe_auth.log')  # Log file for persistent record
    ]
)
logger = logging.getLogger(__name__)


def get_latest_petsafe_code(email_address, app_password, wait_time=20):
    try:
        logger.info(f"Attempting to connect to Gmail IMAP for {email_address}")
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(email_address, app_password)
        logger.info(f"Successfully logged into Gmail account: {email_address}")
        
        # Verbose inbox selection
        mailboxes_to_try = ["INBOX", "[Gmail]/Primary", "Primary"]
        for mailbox in mailboxes_to_try:
            try:
                logger.debug(f"Attempting to select mailbox: {mailbox}")
                status, msgs = mail.select(mailbox)
                if status == 'OK':
                    logger.info(f"Successfully selected mailbox: {mailbox}")
                    break
            except Exception as e:
                logger.warning(f"Failed to select mailbox {mailbox}: {str(e)}")
        else:
            raise Exception("Could not find a valid inbox after trying multiple options")

        # Wait for potential email arrival
        if wait_time:
            logger.info(f"Waiting {wait_time} seconds for email to arrive")
            time.sleep(wait_time)
       
        # Comprehensive email search
        logger.debug("Searching for unread emails")
        _, unread_messages = mail.search(None, 'UNSEEN')
        
        logger.debug("Searching for PetSafe emails")
        _, petsafe_messages = mail.search(None, 'FROM', 'no-reply@directory.cloud.petsafe.net', 'SINCE', (datetime.now() - timedelta(hours=1)).strftime('%d-%b-%Y'))

        logger.debug(f"Unread messages: {unread_messages}")
        logger.debug(f"PetSafe messages: {petsafe_messages}")

        if not petsafe_messages[0]:
            raise Exception("No PetSafe verification emails found")

        # Detailed email processing
        email_ids = petsafe_messages[0].split()
        email_dates = []
        for email_id in email_ids:
            logger.debug(f"Processing email ID: {email_id}")
            _, msg_data = mail.fetch(email_id, "(RFC822)")
            email_message = email.message_from_bytes(msg_data[0][1])
            date_str = email_message['Date']
            date = email.utils.parsedate_to_datetime(date_str)
            email_dates.append((email_id, date))
        
        # Sort and get latest email
        latest_email = sorted(email_dates, key=lambda x: x[1])[-1]
        latest_email_id = latest_email[0]
        
        # Preserve original email flags
        _, msg_flags = mail.fetch(latest_email_id, '(FLAGS)')
        original_flags = msg_flags[0].decode().split('FLAGS (')[-1].split(')')[0]
        logger.debug(f"Original email flags: {original_flags}")
        
        # Fetch and process email content
        _, msg_data = mail.fetch(latest_email_id, "(RFC822)")
        email_message = email.message_from_bytes(msg_data[0][1])
        
        code = None
        if email_message.is_multipart():
            for part in email_message.walk():
                if part.get_content_type() == "text/plain":
                    body = part.get_payload(decode=True).decode()
                    logger.debug(f"Email body: {body}")
                    
                    patterns = [
                        r'verification code is: (\d{6})',
                        r'Your 6-Digit PIN is:\s*(\d{6})',
                        r'code:\s*(\d{6})'
                    ]
                    for pattern in patterns:
                        match = re.search(pattern, body)
                        if match:
                            code = match.group(1)
                            logger.info(f"Found verification code using pattern: {pattern}")
                            break
                    if code:
                        break

        # Restore message read status
        if '\\Seen' not in original_flags:
            logger.debug("Restoring email to unread status")
            mail.store(latest_email_id, '-FLAGS', '\\Seen')

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
        logger.info("Initializing PetSafe authentication")
        client = PetSafeClient(email=login_email)
        
        if not debug_only:
            logger.info("Requesting verification code from PetSafe")
            client.request_code()
            wait_time = 30
        else:
            logger.info("Debug mode: Skipping code request")
            wait_time = 0
        
        logger.info(f"Retrieving verification code (wait time: {wait_time} seconds)")
        code = get_latest_petsafe_code(retrieve_email, app_password, wait_time=wait_time)
        logger.info(f"Successfully retrieved verification code")
        
        if debug_only:
            return {"debug_code": code}
        
        logger.info("Requesting authentication tokens")
        response = client.request_tokens_from_code(code)
        logger.debug(f"Full token response: {json.dumps(response, indent=2)}")
        
        if 'AuthenticationResult' not in response:
            logger.error("Authentication failed: AuthenticationResult missing")
            raise Exception(f"Invalid response: {json.dumps(response, indent=2)}")
        
        token = response['AuthenticationResult']
        logger.info("Authentication successful")
        return {
            "id_token": token['IdToken'],
            "refresh_token": token['RefreshToken'],
            "access_token": token['AccessToken']
        }
    
    except Exception as e:
        logger.error(f"Authentication failed: {str(e)}")
        logger.error(traceback.format_exc())
        raise

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--debug', action='store_true', help='Check existing emails only')
    args = parser.parse_args()

    # Load configuration
    config_path = os.path.join(os.path.dirname(__file__), 'config.yaml')
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)

    try:
        tokens = authenticate_petsafe(
            config['login_email'],
            config['retrieve_email'],
            config['app_password'], 
            debug_only=args.debug
        )
        
        if args.debug:
            print(tokens['debug_code'])
        else:
            token_file = os.path.join(os.path.dirname(__file__), 'tokens.json')
            if os.path.exists(token_file):
                os.remove(token_file)
                logger.info("Previous tokens.json file deleted")
            
            token_data = {
                "id_token": tokens['id_token'],
                "refresh_token": tokens['refresh_token'],
                "access_token": tokens['access_token'],
                "email": config['login_email'],
                "token_expires": time.time() + 3600  # Tokens typically expire in 1 hour
            }
            
            with open(token_file, 'w') as f:
                json.dump(token_data, f, indent=2)
            logger.info("New tokens.json file created")
            
    except Exception as e:
        logger.error(f"Operation failed: {str(e)}")
        raise