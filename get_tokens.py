import imaplib
import email
import email.utils
import time
from petsafe_smartfeed.client import PetSafeClient
import re
import logging
import argparse
import os

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def get_latest_petsafe_code(email_address, app_password, wait_time=20):
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(email_address, app_password)
    logger.info(f"Successfully logged into Gmail account: {email_address}")
    
    # Try different possible inbox names for Gmail
    for mailbox in ["INBOX", "[Gmail]/Primary", "Primary"]:
        try:
            status, msgs = mail.select(mailbox)
            if status == 'OK':
                break
        except Exception as e:
            continue
    else:
        raise Exception("Could not find a valid inbox")

    if wait_time:
        time.sleep(wait_time)
       
    # First search for unread emails
    _, messages = mail.search(None, 'UNSEEN')
    if messages[0]:
        email_ids = messages[0].split()[-3:]  # Get last 3 unread emails

    # Then search for PetSafe emails
    _, messages = mail.search(None, 'FROM no-reply@directory.cloud.petsafe.net')
   
    if not messages[0]:
        raise Exception("No verification emails found")

    email_ids = messages[0].split()
    email_dates = []
    for email_id in email_ids:
        _, msg_data = mail.fetch(email_id, "(RFC822)")
        email_message = email.message_from_bytes(msg_data[0][1])
        date_str = email_message['Date']
        date = email.utils.parsedate_to_datetime(date_str)
        email_dates.append((email_id, date))
    
    # Sort by date and get latest
    latest_email = sorted(email_dates, key=lambda x: x[1])[-1]
    latest_email_id = latest_email[0]
    
    # Get original flags
    _, msg_flags = mail.fetch(latest_email_id, '(FLAGS)')
    original_flags = msg_flags[0].decode().split('FLAGS (')[-1].split(')')[0]
    
    _, msg_data = mail.fetch(latest_email_id, "(RFC822)")
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

    # Restore the message as unread if it was unread before
    if '\\Seen' not in original_flags:
        mail.store(latest_email_id, '-FLAGS', '\\Seen')

    mail.logout()
    if not code:
        raise Exception("Code not found in email")

    return code

def authenticate_petsafe(email_address, app_password, debug_only=False):
    if not debug_only:
        client = PetSafeClient(email=email_address)
        logger.info("Requesting verification code...")
        client.request_code()
        wait_time = 30
    else:
        wait_time = 0
    
    code = get_latest_petsafe_code(email_address, app_password, wait_time=wait_time)
    logger.info(f"Found verification code: {code}")
    
    if debug_only:
        return {"debug_code": code}
    
    logger.info("Requesting tokens with verification code...")    
    try:
        token = client.request_tokens_from_code(code)
        logger.info("Token request successful")
        return {
            "id_token": client.id_token,
            "refresh_token": client.refresh_token,
            "access_token": client.access_token
        }
    except Exception as e:
        logger.error(f"Token request failed: {str(e)}")
        logger.error(f"Response content: {getattr(e, 'response', 'No response available')}")
        raise

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--debug', action='store_true', help='Check existing emails only')
    args = parser.parse_args()

    EMAIL = "luca.avalle@gmail.com"
    APP_PASSWORD = "tgec euvj rvjw htfr"  # Your Gmail App Password
    
    try:
        tokens = authenticate_petsafe(EMAIL, APP_PASSWORD, debug_only=args.debug)
        
        if args.debug:
            print(tokens['debug_code'])
        else:
            if os.path.exists('codes.txt'):
                os.remove('codes.txt')
                print("Previous codes.txt file deleted")
                
            with open('codes.txt', 'w') as f:
                f.write(f"id_token: {tokens['id_token']}\n")
                f.write(f"refresh_token: {tokens['refresh_token']}\n")
                f.write(f"access_token: {tokens['access_token']}\n")
            print("New codes.txt file created")
    except Exception as e:
        logger.error(f"Operation failed: {str(e)}")
        raise