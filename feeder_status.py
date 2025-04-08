# --- feeder_status.py ---
import imaplib
import email
import email.utils
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
from datetime import datetime, timedelta, timezone # Added timezone
import subprocess
import yaml
import jwt
from email.header import decode_header

# ==============================================================================
# Logging Setup (Keep as is)
# ==============================================================================
class SensitiveDataFilter(logging.Filter):
    # ... (keep as is) ...
    def filter(self, record):
        if 'Making request' in str(record.msg): return False
        if 'InitiateAuth' in str(record.msg): return False
        return True

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
logger.addFilter(SensitiveDataFilter())
boto_logger = logging.getLogger('botocore')
boto_logger.addFilter(SensitiveDataFilter())
boto_logger.setLevel(logging.WARNING)
os.environ['AWS_EC2_METADATA_DISABLED'] = 'true'
try:
    boto3.Session(aws_access_key_id='none', aws_secret_access_key='none')
    boto_config = botocore.config.Config(connect_timeout=5, retries={'max_attempts': 0})
except Exception as e: logger.warning(f"Could not configure dummy boto3 session: {e}")

# ==============================================================================
# Constants (Keep as is)
# ==============================================================================
LAST_API_CALL_LOG = 'last_api_call.log'
API_CALL_INTERVAL = 1 * 60
STATE_FILE = 'food_level_state.json'
RAW_HISTORY_FILE = 'raw_feed_messages.json'
DEFAULT_FULL_SENSOR_THRESHOLD = 25000
FOOD_LOW_OVERRIDE_GRAMS = 80.0 # Grams assumed when 'is_food_low' first becomes true
FOOD_LOW_LOOKBACK_DAYS = 4 # How many days to look back for the first low alert

# ==============================================================================
# Helper Functions (Keep as is)
# ==============================================================================
def can_make_api_call():
    # ... (keep as is) ...
    if os.path.exists(LAST_API_CALL_LOG):
        try:
            with open(LAST_API_CALL_LOG, 'r') as f: last_call_time = float(f.read().strip())
            elapsed_time = time.time() - last_call_time
            if elapsed_time < API_CALL_INTERVAL: return False, API_CALL_INTERVAL - elapsed_time
        except (ValueError, IOError) as e: logger.warning(f"Could not read {LAST_API_CALL_LOG}: {e}")
    return True, 0

def log_api_call():
    # ... (keep as is) ...
    try:
        with open(LAST_API_CALL_LOG, 'w') as f: f.write(str(time.time()))
        logger.debug(f"API call time logged to {LAST_API_CALL_LOG}")
    except IOError as e: logger.error(f"Could not write {LAST_API_CALL_LOG}: {e}")

def save_raw_results(feed_messages, filename=RAW_HISTORY_FILE):
    # ... (keep as is) ...
     try:
        with open(filename, 'w') as f: json.dump(feed_messages, f, indent=2)
        logger.info(f"Raw results saved to {filename}")
     except (IOError, TypeError) as e: logger.error(f"Failed to save raw results: {str(e)}")

def load_raw_results(filename=RAW_HISTORY_FILE):
    # ... (keep as is) ...
    logger.debug(f"Attempting to load raw results from {filename}")
    try:
        with open(filename, 'r') as f: data = json.load(f); return data
    except FileNotFoundError: logger.error(f"{filename} not found."); return []
    except json.JSONDecodeError as e: logger.error(f"{filename} malformed: {e}."); return []
    except Exception as e: logger.error(f"Unexpected error loading {filename}: {e}", exc_info=True); return []

def load_food_state():
    # ... (keep as is) ...
    logger.debug(f"Attempting to load food state from {STATE_FILE}")
    default_state = {'remaining_grams': -1.0, 'last_processed_ts': 0.0, 'last_refill_ts': 0.0}
    try:
        with open(STATE_FILE, 'r') as f: state = json.load(f)
        valid_state = {
            'remaining_grams': float(state.get('remaining_grams', -1.0)),
            'last_processed_ts': float(state.get('last_processed_ts', 0.0)),
            'last_refill_ts': float(state.get('last_refill_ts', 0.0))
        }
        refill_dt_str = datetime.fromtimestamp(valid_state['last_refill_ts'], tz=timezone.utc).isoformat() if valid_state['last_refill_ts'] > 0 else "Never"
        logger.info(f"Loaded food state: {valid_state['remaining_grams']:.1f}g, last_proc: {datetime.fromtimestamp(valid_state['last_processed_ts'], tz=timezone.utc).isoformat()}, last_refill: {refill_dt_str}")
        return valid_state
    except FileNotFoundError: logger.info(f"{STATE_FILE} not found."); return default_state
    except (json.JSONDecodeError, TypeError, ValueError) as e: logger.error(f"Error reading {STATE_FILE}: {e}."); return default_state

def save_food_state(remaining_grams, last_processed_ts, last_refill_ts):
    # ... (keep as is) ...
    state = {'remaining_grams': round(remaining_grams, 2), 'last_processed_ts': last_processed_ts, 'last_refill_ts': last_refill_ts}
    try:
        with open(STATE_FILE, 'w') as f: json.dump(state, f, indent=2)
        refill_dt_str = datetime.fromtimestamp(last_refill_ts, tz=timezone.utc).isoformat() if last_refill_ts > 0 else "Never"
        proc_dt_str = datetime.fromtimestamp(last_processed_ts, tz=timezone.utc).isoformat()
        logger.info(f"Saved food state: {state['remaining_grams']:.1f}g, last_proc: {proc_dt_str}, last_refill: {refill_dt_str}")
    except (IOError, TypeError) as e: logger.error(f"Failed to save {STATE_FILE}: {e}")

# ==============================================================================
# Token Handling (Keep as is)
# ==============================================================================
def load_tokens():
    # ... (keep the working version from previous steps) ...
    login_email = None; config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.yaml')
    try:
        with open(config_path, 'r') as f: _config = yaml.safe_load(f)
        login_email = _config.get('login_email')
        if not login_email: raise ValueError("'login_email' missing from config")
        logger.debug(f"Using configured login email: {login_email}")
    except Exception as e: logger.critical(f"CRITICAL: Failed to load login_email: {e}", exc_info=True); sys.exit(1)

    tokens_path = 'tokens.json'
    try:
        logger.debug(f"Attempting to load tokens from {tokens_path}")
        with open(tokens_path, 'r') as f: tokens = json.load(f)
        logger.info(f"Loaded existing tokens from {tokens_path}")
        if tokens.get('email') != login_email: raise ValueError("Token email mismatch")
        if time.time() > tokens.get('token_expires', 0): raise ValueError("Tokens expired")
        if not all(k in tokens for k in ['id_token', 'access_token', 'refresh_token']): raise ValueError("Incomplete tokens")
        logger.info(f"Existing tokens for {tokens['email']} appear valid.")
        return tokens
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
        if isinstance(e, FileNotFoundError): logger.info(f"{tokens_path} not found.")
        elif isinstance(e, json.JSONDecodeError): logger.error(f"{tokens_path} malformed: {e}. Regenerating.")
        else: logger.info(f"Tokens invalid/expired ({e}). Refreshing.")

        print("\nAttempting token generation/refresh via get_tokens.py...")
        script_dir = os.path.dirname(os.path.abspath(__file__)); get_tokens_path = os.path.join(script_dir, 'get_tokens.py')
        try:
            logger.info(f"Running subprocess: {sys.executable} {get_tokens_path}")
            result = subprocess.run(
                [sys.executable, get_tokens_path], check=True, text=True, capture_output=True, timeout=90
            )
            logger.info("get_tokens.py completed.")
            print(f"--- get_tokens.py output ---\n{result.stdout.strip()}")
            if result.stderr.strip(): print(f"--- get_tokens.py errors ---\n{result.stderr.strip()}")
            print("----------------------------")

            codes_path = os.path.join(script_dir, 'codes.txt')
            if not os.path.exists(codes_path): raise FileNotFoundError(f"{codes_path} missing after get_tokens.py run.")

            logger.info(f"Processing {codes_path}")
            tokens_data = {}; required_keys = ['id_token', 'refresh_token', 'access_token', 'email']
            with open(codes_path, 'r') as f_codes: lines = f_codes.readlines()
            for line in lines:
                if ':' in line: key, value = line.split(':', 1); tokens_data[key.strip()] = value.strip()
            if not all(k in tokens_data for k in required_keys): missing = [k for k in required_keys if k not in tokens_data]; raise ValueError(f"Parsed {codes_path} missing: {missing}")
            if tokens_data['email'] != login_email: logger.warning(f"Email in codes.txt ({tokens_data['email']}) != login ({login_email}). Using login.")
            tokens_data['email'] = login_email; tokens_data['token_expires'] = time.time() + 3500

            with open(tokens_path, 'w') as f_json: json.dump(tokens_data, f_json, indent=2)
            logger.info(f"{tokens_path} created/updated for {login_email}.")
            try: os.remove(codes_path); logger.debug(f"Removed {codes_path}")
            except OSError as rm_err: logger.warning(f"Could not remove {codes_path}: {rm_err}")
            return tokens_data
        except subprocess.TimeoutExpired: logger.critical(f"get_tokens.py timed out."); print("\nERROR: Token generation timed out."); sys.exit(1)
        except subprocess.CalledProcessError as e:
             logger.critical(f"get_tokens.py failed (Status {e.returncode}).")
             print(f"\nERROR: get_tokens.py failed. Check logs."); sys.exit(1)
        except Exception as refresh_err: logger.critical(f"Failed token refresh: {refresh_err}", exc_info=True); print(f"\nERROR: Token refresh failed: {refresh_err}"); sys.exit(1)
    except Exception as load_err: logger.critical(f"Unexpected token loading error: {load_err}", exc_info=True); print(f"\nERROR: Token loading failed: {load_err}"); sys.exit(1)

# ==============================================================================
# Data Processing and Calculation
# ==============================================================================

def process_feed_messages(feed_messages):
    """Process feed messages into a standardized format for display"""
    # ... (keep as is - already sorts newest first for display) ...
    processed_messages = []; seven_days_ago_dt = datetime.now(timezone.utc) - timedelta(days=7)
    messages_with_ts = []
    for msg in feed_messages:
         if 'created_at' in msg:
              try:
                   dt_utc = datetime.strptime(msg['created_at'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                   if dt_utc >= seven_days_ago_dt: messages_with_ts.append({'original': msg, 'datetime': dt_utc})
              except (ValueError, TypeError): logger.warning(f"Skipping message - invalid date: {msg.get('created_at')}")
         else: logger.warning(f"Skipping message - missing created_at: {msg}")
    messages_with_ts.sort(key=lambda x: x['datetime'], reverse=True)
    for item in messages_with_ts:
        msg, dt_utc = item['original'], item['datetime']
        if msg.get("message_type") != "FEED_DONE": continue
        formatted_date = dt_utc.strftime('%a %d %b %Y %H:%M')
        payload_raw = msg.get('payload'); payload_dict = {}
        if isinstance(payload_raw, dict): payload_dict = payload_raw
        elif isinstance(payload_raw, str) and payload_raw.strip().startswith('{'):
            try: payload_dict = json.loads(payload_raw)
            except json.JSONDecodeError: pass
        if not isinstance(payload_dict, dict): payload_dict = {}
        clean_msg = {'created_at': formatted_date, 'amount': payload_dict.get('amount', '?')}
        source = payload_dict.get('source', '')
        clean_msg['feed_type'] = 'SCHEDULED' if source == 'schedule' else 'MANUAL'
        processed_messages.append(clean_msg)
    return processed_messages


def calculate_food_status(feed_messages, config, current_is_food_low): # <-- Added current_is_food_low argument
    """
    Calculates remaining food by tracking consumption since the last refill.
    Detects refills based on sensor readings jumping high.
    Applies override if current feeder status is 'Food Low'.
    Returns status dict including last_refill_ts.
    """
    # --- Load parameters from config ---
    try:
        portion_weight = float(config.get('portion_weight', 15))
        feeder_capacity = float(config.get('feeder_capacity', 2770))
        refill_threshold_sensor1 = float(config.get('refill_threshold_sensor1', DEFAULT_FULL_SENSOR_THRESHOLD))
        refill_threshold_sensor2 = float(config.get('refill_threshold_sensor2', DEFAULT_FULL_SENSOR_THRESHOLD))
        logger.debug(f"Food Calc Params: portion={portion_weight}g, capacity={feeder_capacity}g, refill_thr=({refill_threshold_sensor1}, {refill_threshold_sensor2})")
    except (ValueError, TypeError) as e:
         logger.error(f"Invalid feeder params in config: {e}. Using defaults.")
         portion_weight, feeder_capacity = 15.0, 2770.0
         refill_threshold_sensor1, refill_threshold_sensor2 = DEFAULT_FULL_SENSOR_THRESHOLD, DEFAULT_FULL_SENSOR_THRESHOLD

    # --- Load last known state ---
    current_state = load_food_state()
    remaining_grams = current_state['remaining_grams']
    last_processed_ts = current_state['last_processed_ts']
    last_known_refill_ts = current_state['last_refill_ts']
    logger.debug(f"Starting calc with state: {remaining_grams:.1f}g, last_proc_ts={last_processed_ts}, last_refill_ts={last_known_refill_ts}")

    # --- Prepare Messages ---
    valid_messages = []
    for msg in feed_messages:
         if 'created_at' in msg and 'message_type' in msg:
              try:
                   msg_ts = datetime.strptime(msg['created_at'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).timestamp()
                   msg['timestamp'] = msg_ts
                   valid_messages.append(msg)
              except (ValueError, TypeError): logger.warning(f"Skipping message - invalid date: {msg.get('created_at')}")
         else: logger.warning(f"Skipping message - missing fields: {msg}")

    if not valid_messages:
        logger.warning("No valid messages in history to process.")
        save_food_state(0, time.time(), last_known_refill_ts)
        return {'percent_remaining': 0, 'remaining_grams': 0, 'days_of_food_left': 0, 'daily_consumption': 0, 'last_refill_ts': last_known_refill_ts}

    valid_messages.sort(key=lambda x: x['timestamp']) # Sort oldest to newest

    # --- Initialize variables for processing ---
    latest_message_ts_processed = last_processed_ts
    previous_sensor1 = None; previous_sensor2 = None
    refill_detected_ts = 0
    initial_state_was_unknown = (remaining_grams < 0)

    # Find initial sensor state if needed
    if not initial_state_was_unknown:
        for msg in reversed(valid_messages):
            if msg['timestamp'] <= last_processed_ts:
                 payload_data = msg.get('payload', {}); previous_sensor1 = None; previous_sensor2 = None
                 if isinstance(payload_data, str):
                     try: payload_data = json.loads(payload_data)
                     except json.JSONDecodeError: payload_data = {}
                 if isinstance(payload_data, dict):
                     previous_sensor1 = payload_data.get('sensorReading1Infrared')
                     previous_sensor2 = payload_data.get('sensorReading2Infrared')
                     logger.debug(f"Found prev sensor state @ {msg['created_at']}: S1={previous_sensor1}, S2={previous_sensor2}")
                     break


    # --- Process Messages Chronologically ---
    logger.debug(f"Processing {len(valid_messages)} messages since {datetime.fromtimestamp(last_processed_ts, tz=timezone.utc)}...")
    processed_message_count = 0
    for i, msg in enumerate(valid_messages):
        msg_ts = msg['timestamp']
        if not initial_state_was_unknown and msg_ts <= last_processed_ts: continue

        processed_message_count += 1
        msg_type = msg['message_type']; payload_raw = msg.get('payload'); payload_dict = {}
        if isinstance(payload_raw, dict): payload_dict = payload_raw
        elif isinstance(payload_raw, str) and payload_raw.strip().startswith('{'):
            try:
                payload_dict = json.loads(payload_raw)
                if not isinstance(payload_dict, dict): payload_dict = {}
            except json.JSONDecodeError: payload_dict = {}
        elif payload_raw is not None: logger.debug(f"Payload not dict/JSON @ {msg['created_at']}: {type(payload_raw)}")

        current_sensor1 = payload_dict.get('sensorReading1Infrared')
        current_sensor2 = payload_dict.get('sensorReading2Infrared')

        # Refill Detection
        if previous_sensor1 is not None and previous_sensor2 is not None and \
           current_sensor1 is not None and current_sensor2 is not None:
             prev_low1 = previous_sensor1 < (refill_threshold_sensor1 * 0.7)
             prev_low2 = previous_sensor2 < (refill_threshold_sensor2 * 0.7)
             curr_high1 = current_sensor1 > refill_threshold_sensor1
             curr_high2 = current_sensor2 > refill_threshold_sensor2
             if (prev_low1 and curr_high1) and (prev_low2 and curr_high2):
                 logger.info(f"Refill detected @ {msg['created_at']}! Resetting food level.")
                 remaining_grams = feeder_capacity; refill_detected_ts = msg_ts
                 initial_state_was_unknown = False

        if current_sensor1 is not None: previous_sensor1 = current_sensor1
        if current_sensor2 is not None: previous_sensor2 = current_sensor2

        # Establish initial state if needed
        if initial_state_was_unknown:
             logger.warning(f"Initial state unknown. Assuming full @ {msg['created_at']}.")
             remaining_grams = feeder_capacity; initial_state_was_unknown = False

        # Consumption Tracking
        if msg_type == 'FEED_DONE' and not initial_state_was_unknown:
             amount_raw = payload_dict.get('amount'); amount = 0.0
             if isinstance(amount_raw, (int, float)): amount = float(amount_raw)
             if amount > 0:
                 dispensed = amount * portion_weight; old_remaining = remaining_grams
                 remaining_grams -= dispensed
                 logger.debug(f"Feed @ {msg['created_at']}: {amount} portions ({dispensed:.1f}g). Level: {old_remaining:.1f} -> {remaining_grams:.1f}g")
             elif amount_raw is not None: logger.debug(f"Zero/invalid amount '{amount_raw}' @ {msg['created_at']}")

        latest_message_ts_processed = msg_ts
        # End loop

    logger.info(f"Processed {processed_message_count} new messages.")
    final_refill_ts = refill_detected_ts if refill_detected_ts > 0 else last_known_refill_ts

    # Handle final state if still unknown
    if remaining_grams < 0:
         logger.warning("Could not determine food state. Reporting empty.")
         remaining_grams = 0.0
         latest_message_ts_processed = valid_messages[-1]['timestamp'] if valid_messages else time.time()

    remaining_grams_calculated = max(0.0, remaining_grams) # Store calculated value

    # --- Apply Food Low Override ---
    final_remaining_grams = remaining_grams_calculated # Default to calculated value
    override_applied = False
    if current_is_food_low:
         logger.info("Feeder currently reports LOW food. Checking history for override...")
         first_low_alert_ts = 0
         now_ts = time.time()
         lookback_cutoff_ts = now_ts - timedelta(days=FOOD_LOW_LOOKBACK_DAYS).total_seconds()

         # Search for first low alert within lookback period
         for msg in valid_messages: # Already sorted oldest first
              if msg['timestamp'] < lookback_cutoff_ts: continue # Skip messages too old

              payload_low_check = msg.get('payload')
              payload_dict_low = {}
              if isinstance(payload_low_check, dict): payload_dict_low = payload_low_check
              elif isinstance(payload_low_check, str) and payload_low_check.strip().startswith('{'):
                   try: payload_dict_low = json.loads(payload_low_check)
                   except: pass
              if not isinstance(payload_dict_low, dict): payload_dict_low = {}

              # Check the is_food_low field in the payload
              if payload_dict_low.get('is_food_low') == True: # Explicitly check for True
                   first_low_alert_ts = msg['timestamp']
                   logger.info(f"Found first 'is_food_low: true' alert in history @ {msg['created_at']} (within last {FOOD_LOW_LOOKBACK_DAYS} days)")
                   break # Stop searching once found

         if first_low_alert_ts == 0:
              logger.warning(f"Feeder is currently LOW, but no 'is_food_low: true' message found in history within last {FOOD_LOW_LOOKBACK_DAYS} days. Assuming low alert occurred NOW.")
              first_low_alert_ts = now_ts # Assume alert just happened

         # Calculate consumption since the alert time
         grams_consumed_since_alert = 0
         for msg in valid_messages:
              if msg['timestamp'] > first_low_alert_ts and msg['message_type'] == 'FEED_DONE':
                   payload_feed = msg.get('payload', {}); amount = 0.0
                   if isinstance(payload_feed, str):
                        try: payload_feed = json.loads(payload_feed)
                        except: payload_feed = {}
                   if isinstance(payload_feed, dict):
                        amount_raw = payload_feed.get('amount', 0)
                        if isinstance(amount_raw, (int, float)): amount = float(amount_raw)
                   grams_consumed_since_alert += (amount * portion_weight)

         logger.info(f"Calculated {grams_consumed_since_alert:.1f}g consumed since first low alert @ {datetime.fromtimestamp(first_low_alert_ts, tz=timezone.utc).isoformat()}")

         final_remaining_grams = max(0.0, FOOD_LOW_OVERRIDE_GRAMS - grams_consumed_since_alert)
         logger.info(f"Applying 'Food Low' override. Setting remaining grams to {final_remaining_grams:.1f}g (Base: {FOOD_LOW_OVERRIDE_GRAMS}g)")
         override_applied = True
         # Note: We save the *calculated* state (before override) plus the final_refill_ts.
         # The override is applied only for the *current* status report.
         # This prevents the override from permanently corrupting the count-down state.


    # Save state using the calculated value and determined refill time
    save_food_state(remaining_grams_calculated, latest_message_ts_processed, final_refill_ts)

    # --- Calculate Daily Consumption ---
    now_utc = datetime.now(timezone.utc); seven_days_ago_ts = (now_utc - timedelta(days=7)).timestamp()
    recent_feeds = [m for m in valid_messages if m['timestamp'] > seven_days_ago_ts and m['message_type'] == 'FEED_DONE']
    total_portions_last_7_days = 0
    for feed in recent_feeds:
         payload_feed = feed.get('payload', {}); amount = 0.0
         if isinstance(payload_feed, str):
             try: payload_feed = json.loads(payload_feed)
             except: payload_feed = {}
         if isinstance(payload_feed, dict):
              amount_raw = payload_feed.get('amount', 0)
              if isinstance(amount_raw, (int, float)): amount = float(amount_raw)
         total_portions_last_7_days += amount
    total_grams_last_7_days = total_portions_last_7_days * portion_weight
    days_analyzed = 7.0
    if recent_feeds:
         first_feed_ts = min(feed['timestamp'] for feed in recent_feeds)
         actual_duration_seconds = now_utc.timestamp() - first_feed_ts
         days_analyzed = min(7.0, max(1.0, actual_duration_seconds / (24 * 3600)))
    daily_consumption = total_grams_last_7_days / days_analyzed if days_analyzed > 0 else 0
    logger.info(f"Avg daily consumption: {daily_consumption:.1f}g/day")

    # --- Calculate Days Left & Percentage (using the final remaining grams) ---
    safety_margin = 0.9
    days_of_food_left = (final_remaining_grams / daily_consumption) * safety_margin if daily_consumption > 0 else float('inf')
    percent_remaining = (final_remaining_grams / feeder_capacity) * 100 if feeder_capacity > 0 else 0
    percent_remaining = max(0.0, min(100.0, percent_remaining))

    return {
        'percent_remaining': round(percent_remaining, 1),
        'remaining_grams': round(final_remaining_grams, 1),
        'days_of_food_left': round(days_of_food_left, 1) if daily_consumption > 0 else 999,
        'daily_consumption': round(daily_consumption, 1),
        'last_refill_ts': final_refill_ts,
        'override_applied': override_applied # Add flag indicating override happened
    }


# ==============================================================================
# Main Execution Block
# ==============================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check PetSafe Smart Feeder status and food level.")
    parser.add_argument('--dry-run', action='store_true', help='Load cached history instead of making live API calls.')
    parser.add_argument('--verbose', '-v', action='store_true', help='Enable detailed debug logging.')
    parser.add_argument('--force-recalc', action='store_true', help='Ignore saved food level state and recalculate from history.')
    parser.add_argument('--reset-food-level', action='store_true', help='Manually reset food level to full and prompt for manual feed.')
    args = parser.parse_args()

    # --- Setup Logging Level ---
    if args.verbose:
        logger.setLevel(logging.DEBUG); boto_logger.setLevel(logging.DEBUG)
        try: logging.getLogger("urllib3").setLevel(logging.DEBUG); logger.debug("Verbose logging enabled.")
        except Exception: logger.warning("Could not enable detailed urllib3 logging")
    else: logger.setLevel(logging.INFO); boto_logger.setLevel(logging.WARNING); logging.getLogger("urllib3").setLevel(logging.WARNING)

    # --- Load Configuration ---
    config = {}
    try:
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.yaml')
        with open(config_path, 'r') as f: config = yaml.safe_load(f)
        logger.info("Loaded configuration from config.yaml")
        if 'login_email' not in config: raise ValueError("Missing 'login_email' in config")
        # Other config values loaded within functions using defaults if missing
    except Exception as e: logger.critical(f"CRITICAL: Failed to load config.yaml: {e}", exc_info=True); sys.exit(1)

    # --- Handle Manual Reset ---
    if args.reset_food_level:
        try:
            feeder_capacity = float(config.get('feeder_capacity', 2770))
            current_time_ts = time.time()
            # Save state: full capacity, current time for last processed and last refill
            save_food_state(feeder_capacity, current_time_ts, current_time_ts)
            print("\nFood level state has been RESET to Full Capacity.")
            print("Ensure feeder is physically full.")
            print("-> IMPORTANT: Trigger a MANUAL feed now (app/button) <-")
            print("  (This updates sensors for future auto-refill detection)")
            logger.info(f"Manual reset performed. State set to {feeder_capacity}g.")
            sys.exit(0)
        except (ValueError, TypeError): logger.critical("Invalid 'feeder_capacity' in config for reset."); print("ERROR: Invalid 'feeder_capacity'."); sys.exit(1)
        except Exception as reset_err: logger.critical(f"Manual reset failed: {reset_err}", exc_info=True); print(f"ERROR: Reset failed: {reset_err}"); sys.exit(1)

    # --- Force Recalculation? ---
    if args.force_recalc:
         if os.path.exists(STATE_FILE):
              try: os.remove(STATE_FILE); logger.info(f"Force recalc: Removed {STATE_FILE}")
              except OSError as e: logger.error(f"Could not remove state file: {e}")
         else: logger.info("Force recalc: No state file found.")

    # --- Load Tokens ---
    tokens = load_tokens()
    client_email = tokens['email']
    logger.info(f"Using tokens for account: {client_email}")

    # --- Initialize Client ---
    logger.info(f"Initializing PetSafeClient with email: {client_email}")
    client = sf.PetSafeClient(
        email=client_email, id_token=tokens['id_token'],
        refresh_token=tokens['refresh_token'], access_token=tokens['access_token']
    )

    # --- Main Execution Logic ---
    feed_messages = []
    feeder_data = None
    current_feeder_is_low = False # Default

    try:
        if args.dry_run:
            logger.info("Dry Run: Loading cached history.")
            feed_messages = load_raw_results()
            # Cannot get live feeder status in dry run
            logger.warning("Dry run: Cannot get live feeder status for 'Food Low' override.")
        else:
            # --- Rate Limit Check ---
            can_call, wait_time = can_make_api_call()
            if not can_call:
                print(f"API calls rate limited. Wait {int(wait_time // 60)}m {int(wait_time % 60)}s.")
                logger.warning("Rate limited. Using cached history.")
                feed_messages = load_raw_results()
            else:
                # --- Fetch Live Feeder Data FIRST ---
                print("\nAttempting to fetch feeders...")
                try:
                    feeders = client.feeders
                    log_api_call() # Log this call
                    logger.debug(f"Raw feeders response: {feeders}")
                    if feeders:
                        feeder = feeders[0] # Assume first feeder
                        feeder_data = feeder.data
                        current_feeder_is_low = feeder_data.get('is_food_low', False) # Get current low status
                        logger.info(f"Live feeder status: is_food_low = {current_feeder_is_low}")
                    else:
                        print(f"No feeders found for account: {client_email}")
                        logger.warning(f"No feeders returned for {client_email}")
                        # Exit or proceed with only cached history? Let's try cached.
                        feed_messages = load_raw_results()

                except Exception as feeder_err:
                     print(f"Error fetching feeder status: {feeder_err}")
                     logger.error(f"Failed to fetch feeders: {feeder_err}", exc_info=True)
                     # Attempt to use cached history if feeder fetch fails
                     feed_messages = load_raw_results()


                # --- Fetch History (if feeder found) ---
                if feeder_data and not feed_messages: # Only fetch history if not already loaded from cache
                    thing_name = feeder_data.get('thing_name')
                    if not thing_name: logger.error("Feeder missing 'thing_name'. Cannot fetch history.")
                    else:
                        try:
                            # Check rate limit AGAIN before history call
                            can_call_hist, wait_time_hist = can_make_api_call()
                            if not can_call_hist:
                                 print(f"API calls rate limited (before history). Wait {int(wait_time_hist // 60)}m {int(wait_time_hist % 60)}s.")
                                 logger.warning("Rate limited before history fetch. Using cache.")
                                 feed_messages = load_raw_results()
                            else:
                                history_url = f"https://platform.cloud.petsafe.net/smart-feed/feeders/{thing_name}/messages?days=7"
                                history_headers = {"Authorization": tokens["id_token"]}
                                logger.info(f"Fetching history for {thing_name}...")
                                response = requests.get(history_url, headers=history_headers, timeout=20)
                                log_api_call() # Log this call
                                response.raise_for_status()
                                feed_messages = response.json()
                                save_raw_results(feed_messages)
                                logger.info(f"Fetched {len(feed_messages)} messages.")
                        except requests.exceptions.RequestException as e:
                             print(f"\nError fetching history: {e}")
                             logger.error(f"Failed history fetch: {e}", exc_info=True)
                             feed_messages = load_raw_results() # Fallback to cache
                             if feed_messages: logger.warning("Using cached history.")

        # --- Display Feeder Info ---
        if feeder_data:
            settings = feeder_data.get('settings', {})
            print(f"\nFeeder Info:")
            print(f"    Name: {settings.get('friendly_name', 'unnamed')}")
            print(f"    Serial: {feeder_data.get('thing_name')}")
            print(f"    Model: {feeder_data.get('product_name')}")
            print("\n    Live Status:")
            print(f"        Connected: {'Yes' if feeder_data.get('connection_status') == 2 else 'No'}")
            print(f"        Food Low (Feeder): {current_feeder_is_low}") # Show status used for override

        # --- Calculate and Display Food Status ---
        if not feed_messages:
             print("\nNo feed history messages available (live or cached). Cannot calculate status.")
             logger.warning("Cannot calculate food status, no messages.")
        else:
             # *** Pass live status to calculation function ***
             food_status = calculate_food_status(feed_messages, config, current_feeder_is_low)

             print("\n    Food Remaining (Calculated):")
             last_refill_ts = food_status.get('last_refill_ts', 0)
             if last_refill_ts > 0: print(f"        Last Refill: {datetime.fromtimestamp(last_refill_ts, tz=timezone.utc).strftime('%a, %d %b %Y %H:%M %Z')}")
             else: print(f"        Last Refill: Never / State Cleared")
             print(f"        Percentage: {food_status['percent_remaining']:.1f}%")
             print(f"        Est. Weight: {food_status['remaining_grams']:.1f} g")
             print(f"        Avg Consumption: {food_status['daily_consumption']:.1f} g/day")
             days_left = food_status['days_of_food_left']
             days_left_str = f"{days_left:.1f}" if days_left != 999 else "N/A"
             print(f"        Est. Days Left: {days_left_str} days")
             if food_status.get('override_applied'):
                  print("        (Note: Calculation overridden by 'Food Low' status)")

             # Display Recent Processed Events
             print("\n    Recent Feeding Events (Last 7 Days):")
             print("    " + "─" * 40)
             processed_events = process_feed_messages(feed_messages)
             if not processed_events: print("        No feeding events found.")
             else:
                  for event in processed_events[:15]: # Show more events if needed
                       print(f"        {event['created_at']} - {event['amount']} portions ({event['feed_type']})")

    except Exception as e:
        print(f"\nAn unexpected error occurred in main execution: {str(e)}")
        logger.critical(f"Root-level exception: {str(e)}", exc_info=True)