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
import yaml
import json
import traceback
from datetime import datetime, timedelta # Make sure datetime is imported

# Configure logging (Use your existing robust setup)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('petsafe_auth.log')
    ]
)
logger = logging.getLogger(__name__)
logging.getLogger('botocore').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('boto3').setLevel(logging.WARNING)


def load_config():
    """Load configuration from config.yaml file"""
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.yaml')
    try:
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)
            # Ensure required keys are present for THIS flow
            if 'login_email' not in config or 'retrieve_email' not in config or 'app_password' not in config:
                raise ValueError("Config file must contain 'login_email', 'retrieve_email', and 'app_password'")
            # The app_password here MUST be for the retrieve_email account
            logger.info(f"Config loaded: login={config['login_email']}, retrieve={config['retrieve_email']}")
        return config
    except FileNotFoundError:
        logger.error(f"Configuration file not found at {config_path}")
        raise
    except Exception as e:
        logger.error(f"Failed to load or parse config file: {str(e)}")
        raise

# This function logs into the RETRIEVE email address
def get_latest_petsafe_code(retrieve_email_address, retrieve_app_password, wait_time=20):
    """Retrieves the latest PetSafe code from the RETRIEVE email account."""
    logger.info(f"Attempting to connect to Gmail IMAP for {retrieve_email_address} (retrieve account)")
    mail = None # Initialize mail object
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        # *** Login using RETRIEVE email and ITS app password ***
        mail.login(retrieve_email_address, retrieve_app_password)
        logger.info(f"Successfully logged into Gmail account: {retrieve_email_address}")

        status, _ = mail.select("INBOX") # Select inbox of retrieve_email
        if status != 'OK':
            raise Exception(f"Could not select INBOX for {retrieve_email_address}")

        if wait_time:
            logger.info(f"Waiting {wait_time} seconds for forwarded email to arrive in {retrieve_email_address}")
            time.sleep(wait_time)

        # Search for recent PetSafe emails
        search_date = (datetime.now() - timedelta(minutes=10)).strftime("%d-%b-%Y")
        status, messages = mail.search(None,
                                     '(FROM "no-reply@directory.cloud.petsafe.net" SINCE {date})'.format(date=search_date)) # Correct IMAP search syntax

        if status != 'OK':
             raise Exception(f"IMAP search command failed for {retrieve_email_address}")
        if not messages or not messages[0]:
            logger.warning(f"No recent PetSafe verification emails found in {retrieve_email_address}.")
            raise Exception(f"No recent PetSafe verification emails found in {retrieve_email_address}")

        email_ids = messages[0].split()
        latest_email_id = email_ids[-1] # Get the most recent one
        logger.info(f"Fetching latest matching email with ID: {latest_email_id.decode()} from {retrieve_email_address}")

        # Fetch email content
        _, msg_data = mail.fetch(latest_email_id, "(RFC822)")
        if not msg_data or not msg_data[0] or not isinstance(msg_data[0], tuple):
             raise Exception("Could not fetch email content or unexpected data format")

        email_message = email.message_from_bytes(msg_data[0][1])
        code = None
        body = "" # Initialize body

        # Extract plain text body (same logic as before)
        if email_message.is_multipart():
            for part in email_message.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition"))
                if content_type == "text/plain" and "attachment" not in content_disposition:
                    try: body = part.get_payload(decode=True).decode(); break
                    except Exception as decode_err: logger.warning(f"Could not decode part: {decode_err}"); continue
        else:
             content_type = email_message.get_content_type()
             if content_type == "text/plain":
                 try: body = email_message.get_payload(decode=True).decode()
                 except Exception as decode_err: logger.warning(f"Could not decode non-multipart body: {decode_err}")

        # Search for code in the extracted body (same logic as before)
        if body:
            patterns = [r'verification code is: (\d{6})', r'Your 6-Digit PIN is:\s*(\d{6})', r'code:\s*(\d{6})']
            for pattern in patterns:
                match = re.search(pattern, body)
                if match: code = match.group(1); logger.info(f"Found code using pattern: {pattern}"); break
        else:
             logger.warning("Email body was empty or could not be decoded as text/plain.")


        if not code:
            logger.error("Verification code not found in the email body.")
            raise Exception("Verification code not found in email")

        return code

    except imaplib.IMAP4.error as imap_err:
         # This error means login to retrieve_email failed
         logger.error(f"IMAP Error for {retrieve_email_address}: {imap_err}", exc_info=True)
         logger.critical(f"Please check the app_password in config.yaml is correct for {retrieve_email_address}")
         raise
    except Exception as e:
        logger.error(f"Error in get_latest_petsafe_code for {retrieve_email_address}: {str(e)}", exc_info=True)
        raise
    finally:
        # Ensure logout happens even if errors occur
        if mail and mail.state == 'SELECTED':
             try: mail.close()
             except Exception: pass
        if mail and mail.state != 'LOGOUT':
            try: mail.logout()
            except Exception: pass
            logger.info(f"Logged out from {retrieve_email_address}")


# This function requests tokens for login_email using code fetched via retrieve_email
def authenticate_petsafe(login_email, retrieve_email, retrieve_app_password, debug_only=False):
    """Authenticates with PetSafe for login_email by getting code via retrieve_email."""
    client = None # Initialize client reference
    try:
        logger.info(f"Starting PetSafe authentication process for target account: {login_email}")
        # Initialize client with the LOGIN_EMAIL (massavero)
        client = PetSafeClient(email=login_email) # Assign to client variable

        if not debug_only:
            logger.info(f"Requesting verification code via PetSafe API for {login_email}...")
            client.request_code()
            wait_time = 30 # Adjust as needed
            logger.info(f"Code requested. Assuming delivery to {login_email} and forwarding to {retrieve_email}. Waiting {wait_time}s...")
        else:
            logger.info("Debug mode: Skipping PetSafe code request.")
            wait_time = 0

        logger.info(f"Attempting to retrieve code from {retrieve_email} mailbox...")
        # *** Get code using RETRIEVE_EMAIL and its APP_PASSWORD ***
        code = get_latest_petsafe_code(retrieve_email, retrieve_app_password, wait_time=wait_time)
        logger.info(f"Verification code retrieved from {retrieve_email}: {code}")

        if debug_only:
            return {"debug_code": code}

        logger.info("Waiting 5 seconds before using the code...")
        time.sleep(5)

        logger.info(f"Requesting authentication tokens from PetSafe for {login_email} using code {code}...")
        # Use the client initialized with login_email to make the token request
        response = None # Initialize response
        try:
            response = client.request_tokens_from_code(code)
            # If the above line succeeds without KeyError, log success
            logger.debug(f"Raw response from request_tokens_from_code: {response}")

            # Proceed with original logic ONLY if AuthenticationResult is present
            if 'AuthenticationResult' not in response or not response.get('AuthenticationResult'):
                 logger.error(f"Authentication failed: 'AuthenticationResult' key missing or empty in response.")
                 logger.error(f"Full API Response: {response}") # Log the problematic response
                 raise Exception(f"Authentication failed: Invalid response structure from PetSafe API")

            token_data = response['AuthenticationResult']
            logger.info("Authentication request successful (tokens received).")
            logger.debug(f"Received IdToken starts with: {token_data.get('IdToken', '')[:30]}...")
            logger.debug(f"Received AccessToken starts with: {token_data.get('AccessToken', '')[:30]}...")

            return {
                "id_token": token_data.get('IdToken'),
                "refresh_token": token_data.get('RefreshToken'),
                "access_token": token_data.get('AccessToken'),
                "email": login_email
            }

        # Catch the specific error from the library or our check above
        except KeyError as ke:
             logger.error(f"Caught KeyError accessing API response. This usually means auth failed.")
             logger.error(f"Full API Response received before error: {response}") # Log the response object
             logger.error(f"KeyError detail: {ke}")
             raise Exception(f"Authentication failed processing API response (KeyError: {ke})")
        except Exception as token_req_err: # Catch other errors during token request
             logger.error(f"Error during client.request_tokens_from_code: {token_req_err}")
             # Check if response was captured before the error
             if response is not None:
                 logger.error(f"API Response (if available): {response}")
             raise # Re-raise the caught exception


    except Exception as e:
        # Catch errors from get_latest_petsafe_code or the token request block
        logger.error(f"Error during PetSafe authentication for {login_email} (retrieving from {retrieve_email}): {str(e)}", exc_info=True)
        raise # Re-raise to be caught in main block

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--debug', action='store_true', help='Retrieve code from email only, do not authenticate')
    args = parser.parse_args()

    LOGIN_EMAIL = None
    RETRIEVE_EMAIL = None
    RETRIEVE_APP_PASSWORD = None # Password for the retrieve_email account

    try:
        config = load_config()
        LOGIN_EMAIL = config['login_email']
        RETRIEVE_EMAIL = config['retrieve_email']
        RETRIEVE_APP_PASSWORD = config['app_password'] # This is password for RETRIEVE_EMAIL
        logger.info(f"Configuration loaded successfully.")
    except Exception as e:
        print(f"Critical Error: Failed to load configuration. Cannot proceed. Error: {str(e)}")
        logger.critical(f"Failed to load configuration from config.yaml: {e}", exc_info=True)
        sys.exit(1)

    try:
        print(f"Attempting PetSafe authentication for {LOGIN_EMAIL} (using {RETRIEVE_EMAIL} for code retrieval)...")
        # Pass the correct variables
        tokens = authenticate_petsafe(LOGIN_EMAIL, RETRIEVE_EMAIL, RETRIEVE_APP_PASSWORD, debug_only=args.debug)

        if args.debug:
            print(f"Debug Mode: Verification code found in {RETRIEVE_EMAIL} is: {tokens.get('debug_code', 'N/A')}")
        else:
            # Basic check that tokens seem present
            if not all(tokens.get(k) for k in ["id_token", "refresh_token", "access_token", "email"]):
                 logger.error("Authentication process did not return all required token fields.")
                 print("Error: Failed to obtain all required tokens.")
                 sys.exit(1)

            output_file = 'codes.txt'
            if os.path.exists(output_file):
                try: os.remove(output_file); logger.info(f"Removed previous {output_file}")
                except OSError as e: logger.warning(f"Could not remove previous {output_file}: {e}")

            # Write codes.txt, labelling with the intended LOGIN_EMAIL
            try:
                with open(output_file, 'w') as f:
                    f.write(f"id_token: {tokens['id_token']}\n")
                    f.write(f"refresh_token: {tokens['refresh_token']}\n")
                    f.write(f"access_token: {tokens['access_token']}\n")
                    f.write(f"email: {tokens['email']}\n") # Write LOGIN_EMAIL here
                print(f"Successfully wrote tokens (intended for {tokens['email']}) to {output_file}")
                logger.info(f"New tokens intended for {tokens['email']} saved to {output_file}")
            except IOError as e:
                 logger.error(f"Failed to write tokens to {output_file}: {e}", exc_info=True)
                 print(f"Error: Could not write tokens to {output_file}.")
                 sys.exit(1)

    except Exception as e:
        print(f"\nOperation failed: {str(e)}")
        # Log the full traceback at critical level before exiting
        logger.critical(f"get_tokens.py failed: {e}", exc_info=True)
        sys.exit(1) # Exit with error status